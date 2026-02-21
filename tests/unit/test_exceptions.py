"""Test custom exceptions."""

import pytest

from src.exceptions import (
    AuthenticationError,
    CodexCodeTelegramError,
    ConfigurationError,
    SecurityError,
)


def test_base_exception():
    """Test base exception."""
    with pytest.raises(CodexCodeTelegramError):
        raise CodexCodeTelegramError("Test error")


def test_configuration_error():
    """Test configuration error inheritance."""
    with pytest.raises(CodexCodeTelegramError):
        raise ConfigurationError("Config error")

    with pytest.raises(ConfigurationError):
        raise ConfigurationError("Config error")


def test_security_error():
    """Test security error inheritance."""
    with pytest.raises(CodexCodeTelegramError):
        raise SecurityError("Security error")

    with pytest.raises(SecurityError):
        raise AuthenticationError("Auth error")
