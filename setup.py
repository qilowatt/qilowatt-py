from setuptools import setup, find_packages

setup(
    name="qilowatt",
    version="1.0.0",
    description="Communication package for Qilowatt inverters",
    author="@tanelvakker",
    author_email="tanel@vakker.org",
    url="https://github.com/qilowatt/qilowatt-py",
    packages=find_packages(),
    install_requires=[
        "paho-mqtt>=1.6.1",
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
    ],
)