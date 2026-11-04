"""Write-capable Google Search Console MCP server.

Separate from the read-only ``search_console_mcp`` server on purpose: this
package holds a credential carrying the read/write ``webmasters`` scope and
therefore must be deployed, credentialed and audited apart from the read-only
one.
"""

__version__ = "0.1.0"