class QilowattException(Exception):
    """Base exception for Qilowatt errors."""
    pass

class ConnectionError(QilowattException):
    """Raised when there is a connection error."""
    pass

class AuthenticationError(QilowattException):
    """Raised when authentication fails."""
    pass

class DataValidationError(QilowattException):
    """Raised when data validation fails."""
    pass