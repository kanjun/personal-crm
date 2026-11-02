class PeopleArchiveError(Exception):
    """Base error for everything in the people_archive library."""


class GatewayUnreachableError(PeopleArchiveError, ConnectionError):
    """The latchkey gateway (credential proxy) could not be reached or refused the call."""


class GatewayRateLimitedError(PeopleArchiveError):
    """The upstream API answered 403/429; the caller should back off and retry."""


class UpstreamApiError(PeopleArchiveError):
    """The upstream API answered with an error the caller cannot retry past."""


class CredentialsExpiredError(PeopleArchiveError):
    """latchkey reports the account's credentials are expired or revoked; the user must reconnect."""


class InvalidArchiveValueError(PeopleArchiveError, ValueError):
    """A primitive (address, id, account) does not satisfy its rule."""


class StoreError(PeopleArchiveError, OSError):
    """The SQLite archive could not be opened or written."""
