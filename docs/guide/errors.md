# Errors, retries and limits

## What can be raised

Everything enaportal raises on purpose derives from `ENAError`, so one
`except ENAError` catches any failure to talk to ENA:

```text
ENAError
├── ENAQueryError         the query was wrong
├── ENAHTTPError          ENA answered with an error status, or not at all
│   ├── ENAConnectionError    never reached ENA, or the response was cut short
│   ├── ENARateLimitError     still throttled (HTTP 429) after backing off
│   └── ENANotFoundError      HTTP 404, such as an unknown accession
├── ENATimeoutError       ENA took longer than the timeout to answer
├── ENASchemaError        the schema could not be read from anywhere
└── ENACheckpointError    a checkpoint directory belongs to another bulk job
```

Arguments that could never work, such as a negative limit or an unknown
manifest format, raise `ValueError` before anything is sent.

### ENAQueryError

The one to catch for "the query was wrong". ENA reports a bad query in several
different ways, and enaportal turns all of them into this one type:

- A mistake enaportal can see locally: an unknown result type, return field or
  search field. See [validation](searching.md#validation).
- HTTP 400, which the Portal uses for an unknown field, an unknown result type,
  a malformed query or an unsupported parameter, and the Browser for a
  malformed accession or a format the record lacks.
- HTTP 200 with an error message as the body, which is how the Browser's text
  search rejects a query.
- HTTP 500 with `Unknown search field`, which is how the Portal's `/count`
  reports a field it does not know. It is a query error dressed as a server
  fault, so it is raised at once rather than retried.

The message is ENA's own explanation where ENA gave one. `url` and `body` hold
the request and the start of the response.

### ENAHTTPError and its subclasses

`status_code`, `url` and `body` say what happened. A 404 is raised as
`ENANotFoundError`, which the Browser uses for an accession it has never heard
of. A 5xx that survives its retries is raised as `ENAHTTPError` itself.

## Retries

Some failures are worth retrying and some are not:

| Failure | Retried | Why |
|---|---|---|
| Connection error, including a connect timeout | Yes | Transient, and nothing was sent |
| HTTP 5xx | Yes | Usually transient on ENA's side |
| HTTP 429 | Yes, after at least a second | ENA is throttling, not refusing |
| HTTP 4xx other than 429 | No | The request is wrong and will stay wrong |
| Read timeout | No | ENA accepted the request and is still working on it |
| A response cut short while streaming | No | Rows already read have been handed on |

A request is tried up to four times in all. The wait between tries grows
exponentially, from about a quarter of a second to about two, with random
jitter, so that the threads of a bulk fetch do not all retry at the same
moment. After an HTTP 429 the wait is at least one second, the width of ENA's
rate window, and honours a `Retry-After` header up to a minute if ENA ever
sends one.

A read timeout is not retried because it usually means the query is large and
slow, not that it failed. Sending it again adds load and waits just as long.
Raise the timeout instead, or split the work with
[bulk retrieval](bulk.md).

## Timeouts

The default timeouts allow a long download and fail fast on a dead
connection:

| Phase | Default |
|---|---|
| Connect | 10 seconds |
| Read, between chunks of the response | 300 seconds |
| Write | 30 seconds |
| Waiting for a free connection | 10 seconds |

`PortalClient(timeout=60)` or `BrowserClient(timeout=60)` sets all four to the
same number of seconds.

## Rate limit

ENA allows [50 requests per second](https://ena-docs.readthedocs.io/en/latest/retrieval/programmatic-access.html)
from one address, across its APIs, and rejects the excess with HTTP 429. It
sends no headers saying how close a client is to the limit.

Each client spaces its requests to stay under **25 per second**, half of the
limit, because the budget belongs to your address and the client cannot see
what else is using it. The spacing is shared by every thread of one client,
which is what keeps a bulk fetch and its bisection inside a single budget. A
loop over thousands of `count` calls needs no throttling of its own.

The spacing is per client. The module-level functions share one Portal client
and one Browser client, and each has its own budget. Several clients, or
several processes, on one machine do not coordinate, so keep them few if they
run at the same time.

## Warnings

Where enaportal can carry on but you should know something, it issues a
Python warning rather than an error:

- The schema came from an expired cache or from the packaged snapshot, because
  ENA could not be reached.
- A bulk plan cannot resume, partitions on `last_updated`, has partitions over
  the threshold, or cannot reach some matching rows.
- A Browser fetch returned fewer records than accessions.

To make them errors, for a pipeline that must not proceed on partial data:

```python
import warnings

warnings.simplefilter("error")
```

That applies to every warning in the process, not only enaportal's.

The command line prints them to stderr, prefixed `enaportal: warning:`, and
`-q` hides them.
