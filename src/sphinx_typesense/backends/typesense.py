"""Typesense search backend implementation.

This module provides the TypesenseBackend class for server-based search
using Typesense Server or Typesense Cloud.

The backend:
    1. Parses HTML files from the Sphinx build output
    2. Extracts hierarchical content (h1 > h2 > h3 > h4 > text)
    3. Creates Typesense documents with proper weighting
    4. Bulk imports documents into Typesense

Schema:
    Documents follow the DocSearch schema for compatibility:
        - hierarchy.lvl0-3: Heading hierarchy
        - content: Paragraph/list item text
        - url: Full URL with anchor
        - type: Document type (lvl0, lvl1, content, etc.)
        - weight/item_priority: Ranking weights

Example:
    Manual indexing (for custom builds)::

        from sphinx_typesense.backends.typesense import TypesenseBackend

        backend = TypesenseBackend(app)
        count = backend.index_all()
        print(f"Indexed {count} documents")

"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup
from sphinx.util import logging
from typesense.client import Client as TypesenseClient
from typesense.exceptions import (
    HTTPStatus0Error,
    ObjectNotFound,
    RequestUnauthorized,
    ServiceUnavailable,
    Timeout,
)

from sphinx_typesense.backends.base import SearchBackend

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sphinx.application import Sphinx

# Retry configuration for connection attempts
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 1.0
BACKOFF_MULTIPLIER = 2.0

logger = logging.getLogger(__name__)

# Typesense collection schema for documentation
# Follows DocSearch schema for frontend compatibility
DOCS_SCHEMA: dict[str, Any] = {
    "name": "sphinx_docs",  # Configurable via typesense_collection_name
    "fields": [
        # Hierarchical levels (DocSearch compatible - all 7 levels required)
        {"name": "hierarchy.lvl0", "type": "string", "facet": True},
        {"name": "hierarchy.lvl1", "type": "string", "facet": True, "optional": True},
        {"name": "hierarchy.lvl2", "type": "string", "facet": True, "optional": True},
        {"name": "hierarchy.lvl3", "type": "string", "facet": True, "optional": True},
        {"name": "hierarchy.lvl4", "type": "string", "facet": True, "optional": True},
        {"name": "hierarchy.lvl5", "type": "string", "facet": True, "optional": True},
        {"name": "hierarchy.lvl6", "type": "string", "facet": True, "optional": True},
        # Content
        {"name": "content", "type": "string"},
        {"name": "url", "type": "string"},
        {"name": "url_without_anchor", "type": "string", "facet": True},  # Facet for group_by
        {"name": "anchor", "type": "string", "optional": True},
        # Metadata
        {"name": "type", "type": "string", "facet": True},  # "lvl0", "lvl1", "content"
        {"name": "version", "type": "string", "facet": True, "optional": True},
        {"name": "language", "type": "string", "facet": True, "optional": True},
        # Ranking
        {"name": "weight", "type": "int32"},
        {"name": "item_priority", "type": "int64"},
    ],
    "default_sorting_field": "item_priority",
    "token_separators": ["_", "-", "."],
}

# Document type weights for search ranking
# Higher weight = more important in search results
DOC_TYPE_WEIGHTS: dict[str, int] = {
    "lvl0": 100,
    "lvl1": 90,
    "lvl2": 80,
    "lvl3": 70,
    "content": 50,
}

# Document type priorities for default sorting
DOC_TYPE_PRIORITIES: dict[str, int] = {
    "lvl0": 100,
    "lvl1": 90,
    "lvl2": 80,
    "lvl3": 70,
    "content": 50,
}


class TypesenseBackend(SearchBackend):
    """Typesense search backend implementation.

    Provides server-based search using Typesense. Indexes documentation
    content at build time and provides frontend assets for DocSearch UI.

    This class handles the complete indexing pipeline:
        1. Initialize Typesense client from Sphinx config
        2. Ensure collection exists with correct schema
        3. Parse HTML files and extract hierarchical content
        4. Create and bulk import documents

    Attributes:
        name: Backend identifier ("typesense").
        collection_name: Name of the Typesense collection.

    """

    name = "typesense"

    def __init__(self, app: Sphinx) -> None:
        """Initialize the Typesense backend.

        Args:
            app: The Sphinx application instance.

        """
        super().__init__(app)
        self._client: TypesenseClient | None = None
        self.collection_name: str = app.config.typesense_collection_name
        self._server_available: bool | None = None
        logger.debug(
            "sphinx-typesense: TypesenseBackend initialized with collection=%s",
            self.collection_name,
        )

    @property
    def client(self) -> TypesenseClient:
        """Get or create the Typesense client.

        Returns:
            Configured Typesense client instance.

        """
        if self._client is None:
            self._client = self._create_client()
        return self._client

    def _create_client(self) -> TypesenseClient:
        """Initialize Typesense client from Sphinx config.

        Creates a client with configurable timeouts for connection handling.
        The client is configured with reasonable defaults for documentation
        indexing workloads.

        Returns:
            Configured Typesense client instance.

        """
        logger.debug("sphinx-typesense: Creating Typesense client")
        connection_timeout = getattr(self.app.config, "typesense_connection_timeout", 10)
        logger.debug(
            "sphinx-typesense: Client configuration - host=%s, port=%s, protocol=%s, timeout=%ds",
            self.app.config.typesense_host,
            self.app.config.typesense_port,
            self.app.config.typesense_protocol,
            connection_timeout,
        )
        return TypesenseClient(
            {
                "nodes": [
                    {
                        "host": self.app.config.typesense_host,
                        "port": self.app.config.typesense_port,
                        "protocol": self.app.config.typesense_protocol,
                    }
                ],
                "api_key": self.app.config.typesense_api_key,
                "connection_timeout_seconds": connection_timeout,
                "num_retries": 1,  # We handle retries ourselves with backoff
            }
        )

    def _check_connection(self) -> bool:
        """Check if Typesense server is reachable with retry logic.

        Attempts to connect to the Typesense server with exponential backoff.
        Results are cached to avoid repeated connection attempts within the
        same indexing session.

        Returns:
            True if server is reachable and authenticated, False otherwise.

        """
        logger.debug("sphinx-typesense: Checking Typesense server connection")

        # Return cached result if available
        if self._server_available is not None:
            logger.debug("sphinx-typesense: Using cached connection status: %s", self._server_available)
            return self._server_available

        backoff = INITIAL_BACKOFF_SECONDS
        logger.debug(
            "sphinx-typesense: Attempting connection with max_retries=%d, initial_backoff=%.1fs",
            MAX_RETRIES,
            INITIAL_BACKOFF_SECONDS,
        )

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # Health check endpoint validates connectivity and auth
                health = self.client.operations.is_healthy()
                if health:
                    self._server_available = True
                    logger.debug("sphinx-typesense: Typesense server is healthy (attempt %d)", attempt)
                    logger.info("sphinx-typesense: Successfully connected to Typesense server")
                    return True
                # Health check returned False - server not ready
                logger.debug(
                    "sphinx-typesense: Typesense health check returned False (attempt %d/%d)",
                    attempt,
                    MAX_RETRIES,
                )
            except RequestUnauthorized:
                logger.warning(
                    "sphinx-typesense: Authentication failed - invalid API key. "
                    "Please verify typesense_api_key configuration."
                )
                self._server_available = False
                return False
            except ServiceUnavailable:
                logger.debug(
                    "sphinx-typesense: Service unavailable (attempt %d/%d)",
                    attempt,
                    MAX_RETRIES,
                )
            except (Timeout, HTTPStatus0Error):
                logger.debug(
                    "sphinx-typesense: Connection timeout (attempt %d/%d)",
                    attempt,
                    MAX_RETRIES,
                )
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug(
                    "sphinx-typesense: Connection error (attempt %d/%d): %s",
                    attempt,
                    MAX_RETRIES,
                    e,
                )

            # Wait before retry (except on last attempt)
            if attempt < MAX_RETRIES:
                logger.debug("sphinx-typesense: Retrying in %.1f seconds...", backoff)
                time.sleep(backoff)
                backoff *= BACKOFF_MULTIPLIER

        # All retries exhausted
        logger.warning(
            "sphinx-typesense: Server unreachable after %d attempts. "
            "Indexing will be skipped. Check server availability at %s://%s:%s",
            MAX_RETRIES,
            self.app.config.typesense_protocol,
            self.app.config.typesense_host,
            self.app.config.typesense_port,
        )
        self._server_available = False
        return False

    def _ensure_collection(self) -> None:
        """Ensure Typesense collection exists with correct schema.

        Creates the collection if it doesn't exist, or optionally
        drops and recreates if typesense_drop_existing is True.

        """
        logger.debug("sphinx-typesense: Ensuring collection exists: %s", self.collection_name)

        # Check if we should drop existing collection
        if self.app.config.typesense_drop_existing:
            logger.debug("sphinx-typesense: typesense_drop_existing=True, attempting to drop collection")
            try:
                self.client.collections[self.collection_name].delete()
                logger.info("sphinx-typesense: Dropped existing collection: %s", self.collection_name)
            except ObjectNotFound:
                logger.debug("sphinx-typesense: Collection did not exist, nothing to drop")

        # Try to create the collection
        try:
            schema = DOCS_SCHEMA.copy()
            schema["name"] = self.collection_name
            logger.debug("sphinx-typesense: Creating collection with schema")
            self.client.collections.create(schema)  # type: ignore[arg-type]
            logger.info("sphinx-typesense: Created collection: %s", self.collection_name)
        except Exception as e:
            # Collection may already exist - check if it's an "already exists" error
            error_msg = str(e).lower()
            if "already exists" in error_msg:
                logger.debug("sphinx-typesense: Collection already exists: %s", self.collection_name)
            else:
                logger.warning("sphinx-typesense: Failed to create collection: %s", e)
                raise

    def index_all(self) -> int:
        """Index all HTML files from build output.

        Performs connection validation before indexing. If the Typesense
        server is unavailable, returns 0 without failing the build.

        Returns:
            Number of documents indexed, or 0 if server unavailable.

        """
        logger.debug("sphinx-typesense: index_all() invoked")

        # Check connection before attempting to index
        if not self._check_connection():
            logger.info(
                "sphinx-typesense: Skipping indexing - server unavailable. "
                "Documentation build will complete without search indexing."
            )
            return 0

        self._ensure_collection()

        html_dir = Path(self.app.outdir)
        logger.debug("sphinx-typesense: Scanning HTML files in %s", html_dir)
        documents: list[dict[str, Any]] = []
        file_count = 0

        for html_file in html_dir.rglob("*.html"):
            file_count += 1
            documents.extend(self._extract_documents(html_file))

        logger.debug("sphinx-typesense: Processed %d HTML files, extracted %d documents", file_count, len(documents))

        # Bulk import with upsert action
        if documents:
            logger.info(
                "sphinx-typesense: Importing %d documents to collection %s",
                len(documents),
                self.collection_name,
            )
            try:
                result = self.client.collections[self.collection_name].documents.import_(  # type: ignore[call-overload]
                    documents, {"action": "upsert"}
                )
                # Log any import errors
                error_count = 0
                for item in result:
                    if not item.get("success", True):
                        error_count += 1
                        logger.warning(
                            "sphinx-typesense: Failed to index document: %s",
                            item.get("error", "Unknown error"),
                        )
                if error_count > 0:
                    logger.warning("sphinx-typesense: %d documents failed to import", error_count)
            except ServiceUnavailable:
                logger.warning(
                    "sphinx-typesense: Server became unavailable during indexing. Partial indexing may have occurred."
                )
                return 0
            except (Timeout, HTTPStatus0Error) as e:
                logger.warning("sphinx-typesense: Request timed out during bulk import: %s", e)
                return 0
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.warning("sphinx-typesense: Connection lost during indexing: %s", e)
                return 0
        else:
            logger.warning("sphinx-typesense: No documents extracted from HTML files")

        logger.info(
            "sphinx-typesense: Indexing complete - %d documents in collection %s",
            len(documents),
            self.collection_name,
        )
        return len(documents)

    def _extract_documents(self, html_file: Path) -> Iterator[dict[str, Any]]:
        """Extract searchable documents from an HTML file.

        Args:
            html_file: Path to the HTML file to process.

        Yields:
            Document dictionaries ready for Typesense import.

        """
        logger.debug("sphinx-typesense: Extracting documents from %s", html_file.name)
        try:
            soup = BeautifulSoup(html_file.read_text(encoding="utf-8"), "html.parser")
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("sphinx-typesense: Failed to parse HTML file %s: %s", html_file, e)
            return

        # Get theme-specific content selector
        content = self._get_content_element(soup)
        if not content:
            logger.debug("sphinx-typesense: No content element found in %s", html_file)
            return

        # Extract hierarchy
        url_base = self._get_relative_url(html_file)
        hierarchy: dict[str, str] = {"lvl0": "", "lvl1": "", "lvl2": "", "lvl3": ""}

        for element in content.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
            tag = element.name

            for headerlink in element.select(".headerlink"):
                headerlink.decompose()

            text = element.get_text(strip=True)

            if not text:
                continue

            if tag == "h1":
                hierarchy["lvl0"] = text
                hierarchy["lvl1"] = ""
                hierarchy["lvl2"] = ""
                hierarchy["lvl3"] = ""
                yield self._create_document(hierarchy, "", url_base, element, "lvl0")

            elif tag == "h2":
                hierarchy["lvl1"] = text
                hierarchy["lvl2"] = ""
                hierarchy["lvl3"] = ""
                yield self._create_document(hierarchy, "", url_base, element, "lvl1")

            elif tag == "h3":
                hierarchy["lvl2"] = text
                hierarchy["lvl3"] = ""
                yield self._create_document(hierarchy, "", url_base, element, "lvl2")

            elif tag == "h4":
                hierarchy["lvl3"] = text
                yield self._create_document(hierarchy, "", url_base, element, "lvl3")

            elif tag in ("p", "li"):
                yield self._create_document(hierarchy, text, url_base, element, "content")

    def _create_document(
        self,
        hierarchy: dict[str, str],
        content: str,
        url_base: str,
        element: Any,
        doc_type: str,
    ) -> dict[str, Any]:
        """Create a Typesense document from extracted content.

        Args:
            hierarchy: Current heading hierarchy (lvl0-3).
            content: Text content (empty for headings).
            url_base: Base URL without anchor.
            element: BeautifulSoup element for anchor extraction.
            doc_type: Document type (lvl0, lvl1, lvl2, lvl3, content).

        Returns:
            Document dictionary ready for Typesense import.

        """
        anchor = element.get("id", "") or self._find_anchor(element)
        url = f"{url_base}#{anchor}" if anchor else url_base

        # Create unique document ID using SHA256 hash
        doc_id = hashlib.sha256(f"{url}:{content[:100]}".encode()).hexdigest()[:32]

        return {
            "id": doc_id,
            "hierarchy.lvl0": hierarchy["lvl0"],
            "hierarchy.lvl1": hierarchy["lvl1"],
            "hierarchy.lvl2": hierarchy["lvl2"],
            "hierarchy.lvl3": hierarchy["lvl3"],
            "hierarchy.lvl4": hierarchy.get("lvl4", ""),
            "hierarchy.lvl5": hierarchy.get("lvl5", ""),
            "hierarchy.lvl6": hierarchy.get("lvl6", ""),
            "content": content,
            "url": url,
            "url_without_anchor": url_base,
            "anchor": anchor,
            "type": doc_type,
            "version": self.app.config.typesense_doc_version or "",
            "language": self.app.config.language or "en",
            "weight": self._get_weight(doc_type),
            "item_priority": self._get_priority(doc_type),
        }

    def _get_content_element(self, soup: BeautifulSoup) -> Any | None:
        """Get main content element based on theme.

        Args:
            soup: BeautifulSoup parsed HTML.

        Returns:
            The content element, or None if not found.

        """
        selectors = self.app.config.typesense_content_selectors or [
            ".wy-nav-content-wrap",  # RTD theme
            "article.bd-article",  # PyData theme
            ".body",  # Alabaster
            "article[role=main]",  # Furo
            "main",  # Generic fallback
        ]
        logger.debug("sphinx-typesense: Trying content selectors: %s", selectors)

        for selector in selectors:
            element = soup.select_one(selector)
            if element:
                logger.debug("sphinx-typesense: Content element found using selector: %s", selector)
                return element

        logger.debug("sphinx-typesense: No content element matched any selector")
        return None

    def _get_relative_url(self, html_file: Path) -> str:
        """Get URL relative to output directory.

        Args:
            html_file: Absolute path to the HTML file.

        Returns:
            Relative URL path for the file.

        """
        outdir = Path(self.app.outdir)
        try:
            relative = html_file.relative_to(outdir)
            return str(relative)
        except ValueError:
            # File not under outdir, return as-is
            return str(html_file)

    def _find_anchor(self, element: Any) -> str:
        """Find anchor ID for an element.

        Searches for an ID on the element itself, or in child/sibling anchor tags.

        Args:
            element: BeautifulSoup element to search.

        Returns:
            Anchor ID string, or empty string if not found.

        """
        # Check element's own ID
        element_id = element.get("id")
        if element_id:
            return str(element_id)

        # Check for child anchor with ID
        anchor = element.find("a", id=True)
        if anchor:
            anchor_id = anchor.get("id", "")
            return str(anchor_id) if anchor_id else ""

        # Check for child anchor with name attribute (older HTML style)
        anchor = element.find("a", attrs={"name": True})
        if anchor:
            anchor_name = anchor.get("name", "")
            return str(anchor_name) if anchor_name else ""

        # Check previous sibling for permalink anchor (common in Sphinx themes)
        prev_sibling = element.find_previous_sibling()
        if prev_sibling and prev_sibling.name == "a" and prev_sibling.get("id"):
            sibling_id = prev_sibling.get("id", "")
            return str(sibling_id) if sibling_id else ""

        return ""

    def _get_weight(self, doc_type: str) -> int:
        """Assign weight based on document type.

        Args:
            doc_type: Document type (lvl0, lvl1, lvl2, lvl3, content).

        Returns:
            Weight value for search ranking.

        """
        return DOC_TYPE_WEIGHTS.get(doc_type, 50)

    def _get_priority(self, doc_type: str) -> int:
        """Assign priority for default sorting.

        Args:
            doc_type: Document type (lvl0, lvl1, lvl2, lvl3, content).

        Returns:
            Priority value for default sorting.

        """
        return DOC_TYPE_PRIORITIES.get(doc_type, 50)

    def get_js_files(self) -> list[tuple[str, dict[str, str | int]]]:
        """Return Typesense DocSearch JavaScript files.

        Returns:
            List of (filename, attributes) tuples for app.add_js_file().

        """
        return [
            ("typesense-docsearch.js", {"priority": 500}),
            ("typesense-init.js", {"priority": 501}),
        ]

    def get_css_files(self) -> list[str]:
        """Return Typesense DocSearch CSS files.

        Returns:
            List of CSS filenames for app.add_css_file().

        """
        return ["typesense-docsearch.css"]

    def get_config_script(self) -> str:
        """Return inline JavaScript configuration for DocSearch.

        Returns:
            JavaScript code setting window.TYPESENSE_CONFIG.

        """
        config = {
            "collectionName": self.app.config.typesense_collection_name,
            "host": self.app.config.typesense_host,
            "port": str(self.app.config.typesense_port),
            "protocol": self.app.config.typesense_protocol,
            "apiKey": self.app.config.typesense_search_api_key,
            "placeholder": self.app.config.typesense_placeholder,
            "numTypos": self.app.config.typesense_num_typos,
            "perPage": self.app.config.typesense_per_page,
            "filterBy": self.app.config.typesense_filter_by,
            "container": self.app.config.typesense_container,
        }
        config_json = json.dumps(config, indent=2)
        return f"window.TYPESENSE_CONFIG = {config_json};"

    def is_available(self) -> bool:
        """Check if Typesense server is available.

        Returns:
            True if server is reachable and authenticated, False otherwise.

        """
        return self._check_connection()
