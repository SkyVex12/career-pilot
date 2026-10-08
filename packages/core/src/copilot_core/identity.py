"""The user pool behind sign-in: deleting a user's sign-in when their account is deleted
(ARCHITECTURE.md, "Runbooks": `user.delete` removes rows, the S3 prefix, data keys and the
Cognito user).

Users sign in with their email as the username attribute, so Cognito's username is the user's
`sub`, which is the app's user id; `AdminDeleteUser` takes it as is.
"""

import asyncio
import logging
from typing import Any, Protocol
from uuid import UUID

from copilot_core.aws import boto3_client

log = logging.getLogger(__name__)


class UserDirectory(Protocol):
    async def delete_user(self, user_id: UUID) -> bool:
        """Delete the user's sign-in; False if it was already gone."""
        ...


class CognitoUsers:
    def __init__(self, user_pool_id: str, region: str, client: Any = None) -> None:
        self.user_pool_id = user_pool_id
        self.client: Any = client or boto3_client("cognito-idp", region_name=region)

    async def delete_user(self, user_id: UUID) -> bool:
        def call() -> bool:
            try:
                self.client.admin_delete_user(UserPoolId=self.user_pool_id, Username=str(user_id))
            except self.client.exceptions.UserNotFoundException:
                return False
            return True

        return await asyncio.to_thread(call)
