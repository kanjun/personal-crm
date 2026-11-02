from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any

from imbue.imbue_common.mutable_model import MutableModel

from people_archive.primitives import AccountEmail


class GoogleApiGatewayInterface(MutableModel, ABC):
    """Makes authenticated GET calls to Google APIs on behalf of one of the user's accounts."""

    @abstractmethod
    def get_json(
        self, account: AccountEmail, url: str, params: Mapping[str, str] | Sequence[tuple[str, str]]
    ) -> dict[str, Any]:
        """GET the URL with query params as the account and return the parsed JSON body.

        Params are a mapping, or (name, value) pairs when a name repeats (Gmail's labelIds).

        Raises GatewayRateLimitedError on 403/429 (retryable), CredentialsExpiredError
        when the account must be reconnected, UpstreamApiError on other API errors,
        and GatewayUnreachableError when the gateway itself cannot be reached.
        """
