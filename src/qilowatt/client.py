# qilowatt/client.py

import ssl
import json
import threading
from collections import deque
import logging
import paho.mqtt.client as mqtt
from typing import Dict, Any, Callable, List, Optional
from .exceptions import ConnectionError, AuthenticationError
from .base_device import BaseDevice

_logger = logging.getLogger(__name__)

# CONNACK codes meaning the credentials were rejected. MQTT 3.1.1 uses 4 (bad
# user name or password) and 5 (not authorized); paho 2.x reports both through
# a ReasonCode whose value follows the MQTT 5 table: 134 and 135.
_AUTH_FAILURE_CODES = frozenset({4, 5, 134, 135})


def _reason_value(reason_code) -> int:
    """Return the numeric value of a paho ReasonCode or a plain int."""
    value = getattr(reason_code, "value", reason_code)
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _is_auth_failure(reason_code) -> bool:
    return _reason_value(reason_code) in _AUTH_FAILURE_CODES


def _is_success(reason_code) -> bool:
    return _reason_value(reason_code) == 0


class QilowattMQTTClient:
    """Client to handle MQTT communication with Qilowatt server."""

    def __init__(
        self,
        mqtt_username: str,
        mqtt_password: str,
        device: BaseDevice,
        host: str = "mqtt.qilowatt.it",
        port: int = 8883,
        tls: bool = True,
        max_auth_retries: int = 5,
        auth_retry_delay: float = 5.0,
        max_auth_retry_delay: float = 60.0,
    ):
        """Create the client.

        max_auth_retries: how many rejected CONNACKs (bad user name/password or
            not authorized) to tolerate before giving up. Reconnection between
            attempts is handled by paho's own loop, with the delay set by
            reconnect_delay_set (10 s doubling to 60 s).
        auth_retry_delay / max_auth_retry_delay: kept for backwards
            compatibility; paho's reconnect delay now governs the retry pacing.
        """
        self.mqtt_username = mqtt_username
        self.mqtt_password = mqtt_password
        self.device = device

        self.host = host
        self.port = port
        self.tls = tls

        self._max_auth_retries = max(0, max_auth_retries)
        self._auth_retry_delay = max(0.0, auth_retry_delay)
        self._max_auth_retry_delay = max(self._auth_retry_delay, max_auth_retry_delay)

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._connected = False
        self._subscribed = False
        # Guards connection state only. It is never held while a listener or
        # the device hook runs: a listener may call disconnect(), which joins
        # paho's network thread, and that thread may need this lock. Reentrant
        # because disconnect() may be called from inside a transition.
        self._lock = threading.RLock()
        # Listener notifications are queued under the lock and delivered by one
        # thread at a time with no lock held, in transition order.
        self._pending_notifications: deque = deque()
        self._draining_notifications = False
        self._connection_callbacks: List[Callable[[bool], None]] = []
        self._error_callbacks: List[Callable[[Exception], None]] = []
        self._auth_failures = 0
        self._last_error: Optional[Exception] = None
        self._shutdown = False

        # Subscription tracking
        self._pending_subscribe_mid: Optional[int] = None
        self._subscribe_timer: Optional[threading.Timer] = None
        self._subscribe_attempts = 0
        self._max_subscribe_retries = 3
        self._subscribe_timeout = 5.0  # seconds to wait for SUBACK

        # Enable automatic reconnection
        self._client.reconnect_delay_set(min_delay=10, max_delay=60)

        self._setup_client()

        # Set up device callback
        def publish_callback(topic: str, data: Dict[str, Any]):
            if self._client.is_connected():
                payload = json.dumps(data)
                result = self._client.publish(topic, payload)
                if result.rc == mqtt.MQTT_ERR_SUCCESS:
                    _logger.debug(f"Published data to {topic}")
                else:
                    _logger.warning(f"Failed to publish to {topic}: {result.rc}")
            else:
                _logger.warning(f"Cannot publish to {topic}: not connected")
                # Update our internal state if Paho detected disconnection
                self._set_disconnected()
        
        self.device.set_publish_callback(publish_callback)

    @property
    def connected(self) -> bool:
        """Get the current connection state.

        Returns True only when fully connected AND subscribed to command topic.
        This property has no side effects.
        """
        return self._connected

    @property
    def subscribed(self) -> bool:
        """Get the subscription state. True if subscribed to command topic."""
        return self._subscribed

    @property
    def transport_connected(self) -> bool:
        """Get the raw transport connection state from Paho client.

        Returns True if the underlying MQTT transport is connected,
        regardless of subscription state.
        """
        return self._client.is_connected()

    def add_connection_callback(self, callback: Callable[[bool], None]) -> None:
        """Add a callback to be called when connection state changes.
        
        Args:
            callback: A function that takes a boolean parameter (connected state)
        """
        self._connection_callbacks.append(callback)

    def remove_connection_callback(self, callback: Callable[[bool], None]) -> None:
        """Remove a connection state callback."""
        if callback in self._connection_callbacks:
            self._connection_callbacks.remove(callback)

    def add_error_callback(self, callback: Callable[[Exception], None]) -> None:
        """Add a callback invoked when the client gives up on a connection.

        Currently called with an AuthenticationError once the credentials have
        been rejected more than max_auth_retries times. The client has already
        stopped at that point; callers should surface the error to the user.
        """
        self._error_callbacks.append(callback)

    def remove_error_callback(self, callback: Callable[[Exception], None]) -> None:
        """Remove an error callback."""
        if callback in self._error_callbacks:
            self._error_callbacks.remove(callback)

    def _notify_error(self, error: Exception):
        # Snapshot: a callback may unregister itself while handling the error
        for callback in list(self._error_callbacks):
            try:
                callback(error)
            except Exception as e:
                _logger.error(f"Error in error callback: {e}")

    def _notify_connection_change(self, connected: bool):
        """Notify all registered callbacks of connection state change."""
        # Snapshot: a callback may unregister itself while handling the change
        for callback in list(self._connection_callbacks):
            try:
                callback(connected)
            except Exception as e:
                _logger.error(f"Error in connection callback: {e}")

    def _setup_client(self):
        if self.tls:
            self._client.tls_set(cert_reqs=ssl.CERT_NONE)
            self._client.tls_insecure_set(True)

        self._client.username_pw_set(self.mqtt_username, self.mqtt_password)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.on_subscribe = self._on_subscribe

        # Set keep-alive to detect connection issues faster
        self._client.keepalive = 30

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        _logger.debug(f"Connected with result code {reason_code}")
        if _is_success(reason_code):
            self._auth_failures = 0
            self._last_error = None
            self._subscribed = False
            self._subscribe_attempts = 0
            # Subscribe to command topic and wait for SUBACK
            self._attempt_subscribe()
        elif _is_auth_failure(reason_code):
            self._handle_authentication_failure(reason_code)
        else:
            error = ConnectionError(f"Connection failed with result code {reason_code}")
            self._last_error = error
            self._set_disconnected()
            _logger.error(str(error))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        _logger.debug(f"Disconnected with result code {reason_code}")
        self._cancel_subscribe_timer()
        self._subscribed = False
        self._pending_subscribe_mid = None
        self._set_disconnected()

    def _set_connected(self):
        """Mark the client connected (once per connection), let the device send
        what the backend expects at every (re)connect, and notify listeners.

        State changes under the lock; the hook and the listeners run without
        it. Ordering is preserved by the notification queue: a disconnect that
        races this (possible on the subscribe-gave-up path, which runs on a
        timer thread) is delivered after it, so listeners always see True then
        False, never a stale True on a dead connection. A late SUBACK after
        disconnect() is ignored (_shutdown).
        """
        with self._lock:
            if self._shutdown or self._connected:
                return
            self._connected = True
            self._pending_notifications.append(True)
        try:
            self.device.on_connected()
        except Exception:
            _logger.exception("Error in device on_connected hook")
        self._drain_notifications()

    def _set_disconnected(self) -> bool:
        """Mark the client disconnected; notify listeners only on an actual change.

        Returns True if the client had been connected. Keeps a connection that
        never came up, or one already torn down, from emitting a spurious
        "disconnected" notification.
        """
        with self._lock:
            was_connected = self._connected
            self._connected = False
            self._subscribed = False
            if was_connected:
                self._pending_notifications.append(False)
        self._drain_notifications()
        return was_connected

    def _drain_notifications(self):
        """Deliver queued notifications in order, one thread at a time, with no
        lock held during listener calls.

        If another thread is already draining (for instance the thread whose
        listener called disconnect(), which enqueued the False we are looking
        at), leave the item to it: it will be delivered when that listener
        returns, still after the True that preceded it.
        """
        while True:
            with self._lock:
                if self._draining_notifications or not self._pending_notifications:
                    return
                self._draining_notifications = True
                connected = self._pending_notifications.popleft()
            try:
                self._notify_connection_change(connected)
            finally:
                with self._lock:
                    self._draining_notifications = False

    def _on_message(self, client, userdata, msg):
        _logger.debug(f"Message received on {msg.topic}: {msg.payload}")
        if msg.topic == self.device.command_topic:
            self.device.handle_command(msg.payload)

    def _attempt_subscribe(self):
        """Attempt to subscribe to the command topic with timeout tracking."""
        if self._shutdown or not self._client.is_connected():
            return

        self._subscribe_attempts += 1
        topic = self.device.command_topic
        result, mid = self._client.subscribe(topic)

        if result == mqtt.MQTT_ERR_SUCCESS:
            self._pending_subscribe_mid = mid
            _logger.debug(
                f"Subscribe request sent (mid={mid}, attempt {self._subscribe_attempts})"
            )
            # Start timeout timer
            self._cancel_subscribe_timer()
            self._subscribe_timer = threading.Timer(
                self._subscribe_timeout, self._on_subscribe_timeout
            )
            self._subscribe_timer.daemon = True
            self._subscribe_timer.start()
        else:
            _logger.warning(f"Failed to send subscribe request: {result}")
            self._handle_subscribe_failure()

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        """Called when subscription is acknowledged by the broker."""
        _logger.debug(f"Subscription confirmed (mid={mid}, qos={reason_codes})")

        # Check if this is the subscription we're waiting for
        if mid == self._pending_subscribe_mid:
            self._cancel_subscribe_timer()
            self._pending_subscribe_mid = None
            self._subscribed = True
            self._subscribe_attempts = 0

            # Now we're fully connected and subscribed
            self._set_connected()

    def _on_subscribe_timeout(self):
        """Called when subscription confirmation times out."""
        _logger.warning(
            f"Subscribe timeout (attempt {self._subscribe_attempts}/{self._max_subscribe_retries})"
        )
        self._pending_subscribe_mid = None
        self._handle_subscribe_failure()

    def _handle_subscribe_failure(self):
        """Handle failed subscription attempt with retry logic."""
        if self._shutdown or not self._client.is_connected():
            return

        if self._subscribe_attempts < self._max_subscribe_retries:
            _logger.info("Retrying subscription...")
            self._attempt_subscribe()
        else:
            _logger.error(
                f"Subscription failed after {self._max_subscribe_retries} attempts"
            )
            # Still mark as connected so publishing works, but log the issue
            # The client can still publish, just won't receive commands
            self._set_connected()

    def _cancel_subscribe_timer(self):
        """Cancel the subscription timeout timer."""
        if self._subscribe_timer:
            try:
                self._subscribe_timer.cancel()
            except Exception as exc:
                _logger.debug("Error while cancelling subscribe timer: %s", exc)
            finally:
                self._subscribe_timer = None

    def connect(self):
        """Connect to the MQTT broker and start the loop."""
        with self._lock:
            if not self._client.is_connected():
                self._shutdown = False
                self._auth_failures = 0
                self._subscribe_attempts = 0
                self._subscribed = False
                self._last_error = None
                self._client.connect(self.host, self.port, keepalive=30)
                self._client.loop_start()

    def disconnect(self):
        """Disconnect from the MQTT broker and stop the loop.

        Always stops paho's network loop, even when no CONNACK was ever
        received: after connect() the loop thread and socket exist as soon as
        TCP/TLS is up, and a broker that never answers would otherwise leave
        them running. Both paho calls are no-ops when nothing was started.

        The lock is released before loop_stop(): it joins the network thread,
        and that thread may itself be waiting for the lock inside
        _handle_authentication_failure() or _on_disconnect(). Listeners are
        notified once, with no lock held, and only if the client had actually
        been connected; paho invokes _on_disconnect() synchronously from
        disconnect() once the loop is stopped, and that path no longer notifies
        for an already-cleared state.
        """
        with self._lock:
            self._shutdown = True
            self._cancel_subscribe_timer()
            self._pending_subscribe_mid = None
        # Outside the lock: this may notify listeners
        self._set_disconnected()

        try:
            self._client.loop_stop()
        except Exception as exc:
            _logger.debug("loop_stop during disconnect: %s", exc)
        try:
            self._client.disconnect()
        except Exception as exc:
            _logger.debug("disconnect during disconnect: %s", exc)

        self.device.stop_timers()

    def last_error(self) -> Optional[Exception]:
        """Return the most recent connection error, if any."""
        return self._last_error

    def _handle_authentication_failure(self, reason_code=None):
        """Handle a CONNACK that rejected the credentials.

        paho's network loop reconnects on its own after a refused CONNACK, so
        this only counts the failures and stops the client once the limit is
        exceeded. Stopping is final: callers must create a new connection
        after fixing the credentials.
        """
        self._set_disconnected()
        self._auth_failures += 1

        if self._auth_failures <= self._max_auth_retries:
            _logger.warning(
                "Authentication failed (%s, attempt %s/%s); paho will retry",
                reason_code,
                self._auth_failures,
                self._max_auth_retries,
            )
            return

        message = (
            f"Authentication failed after {self._max_auth_retries} retries"
            if self._max_auth_retries
            else "Authentication failed"
        )
        error = AuthenticationError(message)
        self._last_error = error
        _logger.error(message)
        with self._lock:
            self._shutdown = True
            self._cancel_subscribe_timer()
            try:
                self._client.loop_stop()
            except Exception:
                pass
            try:
                self._client.disconnect()
            except Exception:
                pass
        self.device.stop_timers()
        self._notify_error(error)
