"""Configuration handling for sphinx-typesense.

This module manages all Typesense-related configuration values in Sphinx's
conf.py, including validation, defaults, and environment variable support.

Configuration Values:
    Required:
        - typesense_host: Typesense server hostname
        - typesense_port: Typesense server port
        - typesense_protocol: Connection protocol (http/https)
        - typesense_api_key: Admin API key for indexing
        - typesense_search_api_key: Search-only API key for frontend

    Optional:
        - typesense_collection_name: Name of the Typesense collection
        - typesense_doc_version: Documentation version tag
        - typesense_placeholder: Search input placeholder text
        - typesense_num_typos: Typo tolerance level (0-2)
        - typesense_per_page: Results per page
        - typesense_container: CSS selector for search container
        - typesense_filter_by: Default search filter
        - typesense_content_selectors: Theme content selectors
        - typesense_enable_indexing: Enable/disable indexing
        - typesense_drop_existing: Drop collection before reindex

Example:
    In conf.py::

        import os

        typesense_host = "localhost"
        typesense_port = "8108"
        typesense_protocol = "http"
        typesense_api_key = os.environ.get("TYPESENSE_API_KEY", "")
        typesense_search_api_key = os.environ.get("TYPESENSE_SEARCH_KEY", "")

"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from sphinx.errors import ConfigError
from sphinx.util import logging

if TYPE_CHECKING:
    from sphinx.application import Sphinx
    from sphinx.config import Config

logger = logging.getLogger(__name__)

# Default configuration values
DEFAULT_HOST = "localhost"
DEFAULT_PORT = "8108"
DEFAULT_PROTOCOL = "http"
DEFAULT_COLLECTION_NAME = "sphinx_docs"
DEFAULT_PLACEHOLDER = "Search documentation..."
DEFAULT_NUM_TYPOS = 2
DEFAULT_PER_PAGE = 10
DEFAULT_CONTAINER = "#typesense-search"

# Typo tolerance bounds
MIN_NUM_TYPOS = 0
MAX_NUM_TYPOS = 2

# Theme-specific content selectors (in priority order)
DEFAULT_CONTENT_SELECTORS = [
    ".wy-nav-content-wrap",  # RTD theme
    "article.bd-article",  # PyData theme
    ".body",  # Alabaster
    "article[role=main]",  # Furo
    "main",  # Generic fallback
]

# Environment variable names for API keys
ENV_API_KEY = "TYPESENSE_API_KEY"
ENV_SEARCH_API_KEY = "TYPESENSE_SEARCH_API_KEY"

# Valid protocols
VALID_PROTOCOLS = {"http", "https"}

# Valid backends for search
VALID_BACKENDS = {"auto", "typesense", "pagefind"}


def get_effective_backend(config: Config) -> str:
    """Determine which backend to use based on configuration.

    This function resolves the 'auto' backend setting to either 'typesense'
    or 'pagefind' based on the availability of API keys.

    Args:
        config: The Sphinx configuration object.

    Returns:
        'typesense' or 'pagefind' - the effective backend to use.

    Note:
        When backend is 'auto':
        - Returns 'typesense' if an API key is present (config or environment)
        - Returns 'pagefind' if no API key is available

    """
    backend: str = config.typesense_backend
    if backend == "auto":
        # Use typesense if API key is present, otherwise pagefind
        if config.typesense_api_key or os.environ.get(ENV_API_KEY):
            return "typesense"
        return "pagefind"
    return backend


def setup_config(app: Sphinx) -> None:
    """Register Typesense configuration values with Sphinx.

    Args:
        app: The Sphinx application instance.

    Note:
        Configuration values are registered with 'html' as their rebuild
        trigger, meaning changes will cause HTML rebuild.

    """
    logger.debug("sphinx-typesense: Registering configuration values with Sphinx")

    # Backend selection
    app.add_config_value("typesense_backend", "auto", "html")

    # Required settings
    app.add_config_value("typesense_host", DEFAULT_HOST, "html")
    app.add_config_value("typesense_port", DEFAULT_PORT, "html")
    app.add_config_value("typesense_protocol", DEFAULT_PROTOCOL, "html")
    app.add_config_value("typesense_api_key", "", "html")
    app.add_config_value("typesense_search_api_key", "", "html")

    # Optional settings - Collection
    app.add_config_value("typesense_collection_name", DEFAULT_COLLECTION_NAME, "html")
    app.add_config_value("typesense_doc_version", "", "html")

    # Optional settings - Search UI
    app.add_config_value("typesense_placeholder", DEFAULT_PLACEHOLDER, "html")
    app.add_config_value("typesense_num_typos", DEFAULT_NUM_TYPOS, "html")
    app.add_config_value("typesense_per_page", DEFAULT_PER_PAGE, "html")
    app.add_config_value("typesense_container", DEFAULT_CONTAINER, "html")
    app.add_config_value("typesense_filter_by", "", "html")

    # Optional settings - Content extraction
    app.add_config_value("typesense_content_selectors", DEFAULT_CONTENT_SELECTORS, "html")

    # Optional settings - Advanced
    app.add_config_value("typesense_enable_indexing", default=True, rebuild="html")
    app.add_config_value("typesense_drop_existing", default=False, rebuild="html")

    app.add_config_value("typesense_connection_timeout", 10, "html")

    logger.debug("sphinx-typesense: Configuration values registered successfully")


def validate_config(app: Sphinx, config: Config) -> None:  # noqa: ARG001
    """Validate Typesense configuration at build time.

    This function performs comprehensive validation of all Typesense configuration
    values, with support for environment variable fallback for API keys.

    Args:
        app: The Sphinx application instance (unused but required by Sphinx event signature).
        config: The Sphinx configuration object.

    Raises:
        sphinx.errors.ConfigError: If required configuration is missing or invalid.

    Note:
        - API keys can be provided via environment variables (TYPESENSE_API_KEY,
          TYPESENSE_SEARCH_API_KEY) as fallback when not set in conf.py.
        - A warning is issued if admin and search API keys are identical,
          as this may indicate a security concern.
        - When typesense_enable_indexing is False, API keys are not required.
        - When typesense_backend is 'pagefind', Typesense API keys are not required.
        - When typesense_backend is 'auto' and no API keys are present, Pagefind
          will be used and API key validation is skipped.

    """
    logger.debug("sphinx-typesense: Validating configuration")

    # Validate backend setting first
    _validate_backend(config)

    # Resolve API keys with environment variable fallback
    api_key = config.typesense_api_key or os.environ.get(ENV_API_KEY, "")
    search_api_key = config.typesense_search_api_key or os.environ.get(ENV_SEARCH_API_KEY, "")

    # Log API key sources (without exposing actual keys)
    if config.typesense_api_key:
        logger.debug("sphinx-typesense: Admin API key provided via conf.py")
    elif os.environ.get(ENV_API_KEY):
        logger.debug("sphinx-typesense: Admin API key resolved from %s environment variable", ENV_API_KEY)
    else:
        logger.debug("sphinx-typesense: No admin API key configured")

    if config.typesense_search_api_key:
        logger.debug("sphinx-typesense: Search API key provided via conf.py")
    elif os.environ.get(ENV_SEARCH_API_KEY):
        logger.debug("sphinx-typesense: Search API key resolved from %s environment variable", ENV_SEARCH_API_KEY)
    else:
        logger.debug("sphinx-typesense: No search API key configured")

    # Update config with resolved values for downstream use
    config.typesense_api_key = api_key
    config.typesense_search_api_key = search_api_key

    # Determine effective backend and log info
    effective_backend = get_effective_backend(config)
    if config.typesense_backend == "auto":
        logger.info(
            "sphinx-typesense: Backend 'auto' resolved to '%s' (API key %s)",
            effective_backend,
            "present" if api_key else "not present",
        )
    else:
        logger.debug("sphinx-typesense: Using '%s' backend", effective_backend)

    # Log effective configuration values (excluding sensitive data)
    logger.debug(
        "sphinx-typesense: Configuration values - host=%s, port=%s, protocol=%s, collection=%s",
        config.typesense_host,
        config.typesense_port,
        config.typesense_protocol,
        config.typesense_collection_name,
    )

    # Skip strict validation if indexing is disabled
    if not config.typesense_enable_indexing:
        logger.info("sphinx-typesense: Indexing disabled, skipping API key validation")
        return

    # Skip Typesense API key validation if using Pagefind backend
    if effective_backend == "pagefind":
        logger.info("sphinx-typesense: Using Pagefind backend, skipping Typesense API key validation")
        # Still validate protocol and numeric settings
        _validate_protocol(config)
        _validate_numeric_settings(config)
        logger.debug("sphinx-typesense: Configuration validation complete")
        return

    # Validate required settings (only when using Typesense backend)
    _validate_required_settings(config, api_key, search_api_key)

    # Validate protocol
    _validate_protocol(config)

    # Warn if admin and search keys are the same
    _check_key_security(api_key, search_api_key)

    # Validate numeric settings
    _validate_numeric_settings(config)

    logger.debug("sphinx-typesense: Configuration validation complete")


def _validate_backend(config: Config) -> None:
    """Validate the backend setting.

    Args:
        config: The Sphinx configuration object.

    Raises:
        ConfigError: If backend is not one of 'auto', 'typesense', or 'pagefind'.

    """
    backend = config.typesense_backend
    logger.debug("sphinx-typesense: Validating backend: %s", backend)
    if backend not in VALID_BACKENDS:
        logger.error(
            "sphinx-typesense: Invalid backend '%s', must be one of: %s",
            backend,
            ", ".join(sorted(VALID_BACKENDS)),
        )
        msg = (
            f"sphinx-typesense: Invalid typesense_backend '{backend}'. "
            f"Must be one of: {', '.join(sorted(VALID_BACKENDS))}"
        )
        raise ConfigError(msg)


def _validate_required_settings(config: Config, api_key: str, search_api_key: str) -> None:
    """Validate that all required settings are present.

    Args:
        config: The Sphinx configuration object.
        api_key: Resolved admin API key.
        search_api_key: Resolved search API key.

    Raises:
        ConfigError: If any required setting is missing.

    """
    logger.debug("sphinx-typesense: Validating required settings")
    missing = []

    if not config.typesense_host:
        missing.append("typesense_host")
    if not config.typesense_port:
        missing.append("typesense_port")
    if not config.typesense_protocol:
        missing.append("typesense_protocol")
    if not api_key:
        missing.append(f"typesense_api_key (or {ENV_API_KEY} environment variable)")
    if not search_api_key:
        missing.append(f"typesense_search_api_key (or {ENV_SEARCH_API_KEY} environment variable)")

    if missing:
        logger.error("sphinx-typesense: Missing required configuration: %s", ", ".join(missing))
        msg = f"sphinx-typesense: Missing required configuration: {', '.join(missing)}"
        raise ConfigError(msg)

    logger.debug("sphinx-typesense: All required settings present")


def _validate_protocol(config: Config) -> None:
    """Validate the protocol setting.

    Args:
        config: The Sphinx configuration object.

    Raises:
        ConfigError: If protocol is not 'http' or 'https'.

    """
    protocol = config.typesense_protocol
    logger.debug("sphinx-typesense: Validating protocol: %s", protocol)
    if protocol not in VALID_PROTOCOLS:
        logger.error(
            "sphinx-typesense: Invalid protocol '%s', must be one of: %s",
            protocol,
            ", ".join(sorted(VALID_PROTOCOLS)),
        )
        msg = (
            f"sphinx-typesense: Invalid typesense_protocol '{protocol}'. "
            f"Must be one of: {', '.join(sorted(VALID_PROTOCOLS))}"
        )
        raise ConfigError(msg)


def _check_key_security(api_key: str, search_api_key: str) -> None:
    """Check for potential security issues with API keys.

    Args:
        api_key: Admin API key.
        search_api_key: Search API key.

    Note:
        This issues a warning but does not raise an error, as there may
        be legitimate use cases for identical keys in development.

    """
    if api_key and search_api_key and api_key == search_api_key:
        logger.warning(
            "sphinx-typesense: Admin API key and search API key are identical. "
            "For production, use a separate search-only key with limited permissions."
        )


def _validate_numeric_settings(config: Config) -> None:
    """Validate numeric configuration settings.

    Args:
        config: The Sphinx configuration object.

    Raises:
        ConfigError: If numeric settings have invalid values.

    """
    logger.debug("sphinx-typesense: Validating numeric settings")

    # Validate num_typos (must be 0-2)
    num_typos = config.typesense_num_typos
    logger.debug("sphinx-typesense: Validating num_typos=%s", num_typos)
    if not isinstance(num_typos, int) or num_typos < MIN_NUM_TYPOS or num_typos > MAX_NUM_TYPOS:
        logger.error(
            "sphinx-typesense: Invalid num_typos '%s', must be integer between %d and %d",
            num_typos,
            MIN_NUM_TYPOS,
            MAX_NUM_TYPOS,
        )
        msg = (
            f"sphinx-typesense: Invalid typesense_num_typos '{num_typos}'. "
            f"Must be an integer between {MIN_NUM_TYPOS} and {MAX_NUM_TYPOS}."
        )
        raise ConfigError(msg)

    # Validate per_page (must be positive)
    per_page = config.typesense_per_page
    logger.debug("sphinx-typesense: Validating per_page=%s", per_page)
    if not isinstance(per_page, int) or per_page < 1:
        logger.error("sphinx-typesense: Invalid per_page '%s', must be a positive integer", per_page)
        msg = f"sphinx-typesense: Invalid typesense_per_page '{per_page}'. Must be a positive integer."
        raise ConfigError(msg)

    logger.debug("sphinx-typesense: Numeric settings validation complete")
