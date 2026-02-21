"""Test environment-specific configurations."""

from src.config.environments import DevelopmentConfig, ProductionConfig, TestingConfig


def test_development_config():
    """Test development configuration values."""
    config_dict = DevelopmentConfig.as_dict()

    assert config_dict["debug"] is True
    assert config_dict["development_mode"] is True
    assert config_dict["log_level"] == "DEBUG"
    assert config_dict["rate_limit_requests"] == 100
    assert config_dict["enable_telemetry"] is False


def test_testing_config():
    """Test testing configuration values."""
    config_dict = TestingConfig.as_dict()

    assert config_dict["debug"] is True
    assert config_dict["development_mode"] is True
    assert config_dict["database_url"] == "sqlite:///:memory:"
    assert config_dict["approved_directory"] == "/tmp/test_projects"
    assert config_dict["enable_telemetry"] is False
    assert config_dict["codex_timeout_seconds"] == 30
    assert config_dict["rate_limit_requests"] == 1000


def test_production_config():
    """Test production configuration values."""
    config_dict = ProductionConfig.as_dict()

    assert config_dict["debug"] is False
    assert config_dict["development_mode"] is False
    assert config_dict["log_level"] == "INFO"
    assert config_dict["enable_telemetry"] is True
    assert config_dict["codex_max_cost_per_user"] == 5.0
    assert config_dict["rate_limit_requests"] == 5


def test_config_as_dict_excludes_internals():
    """Test that as_dict() excludes internal attributes."""
    config_dict = DevelopmentConfig.as_dict()

    # Should not include dunder methods or callable attributes
    for key in config_dict.keys():
        assert not key.startswith("_")
        assert not callable(getattr(DevelopmentConfig, key))
