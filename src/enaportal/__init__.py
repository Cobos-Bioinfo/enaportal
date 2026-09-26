"""A typed Python client for the ENA Portal and Browser APIs."""

from enaportal._api import (
    bulk_search,
    count,
    default_client,
    filereport,
    plan_partitions,
    refresh_schema,
    related,
    results,
    return_fields,
    search,
    search_fields,
)
from enaportal._version import __version__
from enaportal.bulk import BulkPlan, Partition
from enaportal.errors import (
    ENACheckpointError,
    ENAConnectionError,
    ENAError,
    ENAHTTPError,
    ENAQueryError,
    ENARateLimitError,
    ENASchemaError,
    ENATimeoutError,
)
from enaportal.files import file_urls, to_manifest
from enaportal.portal import PortalClient
from enaportal.schema import Field, Result, SchemaClient

__all__ = [
    "BulkPlan",
    "ENACheckpointError",
    "ENAConnectionError",
    "ENAError",
    "ENAHTTPError",
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
    "file_urls",
    "filereport",
    "plan_partitions",
    "refresh_schema",
    "related",
    "results",
    "return_fields",
    "search",
    "search_fields",
    "to_manifest",
]
