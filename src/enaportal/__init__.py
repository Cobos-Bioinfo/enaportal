"""A typed Python client for the ENA Portal and Browser APIs."""

from enaportal._api import (
    bulk_search,
    count,
    default_client,
    fetch,
    filereport,
    plan_partitions,
    refresh_schema,
    related,
    results,
    return_fields,
    search,
    search_fields,
    textsearch,
    textsearch_count,
)
from enaportal._version import __version__
from enaportal.browser import BrowserClient
from enaportal.bulk import BulkPlan, Partition
from enaportal.errors import (
    ENACheckpointError,
    ENAConnectionError,
    ENAError,
    ENAHTTPError,
    ENANotFoundError,
    ENAQueryError,
    ENARateLimitError,
    ENASchemaError,
    ENATimeoutError,
)
from enaportal.files import file_urls, to_manifest
from enaportal.portal import PortalClient
from enaportal.schema import Field, Result, SchemaClient

__all__ = [
    "BrowserClient",
    "BulkPlan",
    "ENACheckpointError",
    "ENAConnectionError",
    "ENAError",
    "ENAHTTPError",
    "ENANotFoundError",
    "ENAQueryError",
    "ENARateLimitError",
    "ENASchemaError",
    "ENATimeoutError",
    "Field",
    "Partition",
    "PortalClient",
    "Result",
    "SchemaClient",
    "__version__",
    "bulk_search",
    "count",
    "default_client",
    "fetch",
    "file_urls",
    "filereport",
    "plan_partitions",
    "refresh_schema",
    "related",
    "results",
    "return_fields",
    "search",
    "search_fields",
    "textsearch",
    "textsearch_count",
    "to_manifest",
]
