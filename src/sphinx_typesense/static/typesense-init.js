/**
 * sphinx-typesense: Frontend initialization script
 * Initializes DocSearch with Typesense backend
 */
(function() {
  "use strict";

  function initDocSearch() {
    var config = window.TYPESENSE_CONFIG;
    if (!config || !config.collectionName || !config.apiKey) {
      console.warn("sphinx-typesense: Missing configuration");
      return false;
    }

    // Check if docsearch is available
    if (typeof docsearch === "undefined") {
      console.warn("sphinx-typesense: docsearch not loaded yet");
      return false;
    }

    var containerId = config.container || "#typesense-search";
    var container = document.querySelector(containerId);

    // Create container if it doesn't exist
    if (!container) {
      // Theme-specific search containers to hide entirely
      var themeSearchToHide = [
        "form.searchbox",                  // Shibuya - hide entire form
        "#pst-search-dialog",              // PyData - search dialog
        ".search-button-field",            // PyData - search button in header
        ".bd-search",                      // PyData - search form container
        '[role="search"] form',            // Generic
        ".sidebar-search form",            // Alabaster
      ];

      // Hide existing theme search forms
      themeSearchToHide.forEach(function(selector) {
        var elements = document.querySelectorAll(selector);
        elements.forEach(function(el) {
          el.style.display = "none";
        });
      });

      // Find target to insert our search container (parent of search, not search itself)
      var searchTargets = [
        ".sy-head-extra",               // Shibuya header extra area
        ".navbar-persistent--container", // PyData - persistent navbar area
        '[role="search"]',              // RTD, Furo
        ".wy-side-nav-search",          // RTD sidebar
        ".sidebar-search",              // Alabaster
        "header",                       // Fallback
      ];

      var targetParent = null;
      for (var i = 0; i < searchTargets.length; i++) {
        targetParent = document.querySelector(searchTargets[i]);
        if (targetParent) {
          console.log("sphinx-typesense: Found target:", searchTargets[i]);
          break;
        }
      }

      container = document.createElement("div");
      container.id = containerId.replace("#", "");
      container.style.cssText = "display:inline-block;flex:1;";

      if (targetParent) {
        targetParent.insertBefore(container, targetParent.firstChild);
      } else {
        document.body.insertBefore(container, document.body.firstChild);
      }
    }

    try {
      console.log("sphinx-typesense: Initializing DocSearch with config:", {
        container: containerId,
        collection: config.collectionName,
        host: config.host,
        port: config.port,
        protocol: config.protocol,
      });

      docsearch({
        container: containerId,
        typesenseCollectionName: config.collectionName,
        typesenseServerConfig: {
          nodes: [{
            host: config.host || "127.0.0.1",
            port: config.port || 8108,
            protocol: config.protocol || "http",
          }],
          apiKey: config.apiKey,
        },
        typesenseSearchParameters: {
          // DocSearch standard query_by with all hierarchy levels
          query_by: "hierarchy.lvl0,hierarchy.lvl1,hierarchy.lvl2,hierarchy.lvl3,hierarchy.lvl4,hierarchy.lvl5,hierarchy.lvl6,content",
          // Group results by URL (required by DocSearch UI)
          group_by: "url_without_anchor",
          group_limit: 3,
          num_typos: config.numTypos || 2,
          per_page: config.perPage || 10,
          filter_by: config.filterBy || "",
        },
        placeholder: config.placeholder || "Search...",
      });
      console.log("sphinx-typesense: DocSearch initialized successfully");
      return true;
    } catch (e) {
      console.error("sphinx-typesense: Init failed:", e);
      console.error("sphinx-typesense: Error stack:", e.stack);
      return false;
    }
  }

  // Try to init when DOM is ready, with retries for async script loading
  function tryInit(attempts) {
    if (initDocSearch()) return;
    if (attempts > 0) {
      setTimeout(function() { tryInit(attempts - 1); }, 100);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function() { tryInit(20); });
  } else {
    tryInit(20);
  }
})();
