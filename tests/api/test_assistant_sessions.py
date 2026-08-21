from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest

from docreview.api.dependencies import AppDependencies
from docreview.api.main import create_app
from docreview.config.settings import load_settings
from docreview.storage.models import AssistantMessage, AssistantSession
from docreview.storage.postgres.errors import SessionNotFoundError
from tests.trusted_identity import identity_adapter, signed_headers

WORKSPACE_ID = "33333333-3333-4333-8333-333333333333"
OTHER_WORKSPACE_ID = "44444444-4444-4444-8444-444444444444"
SESSION_ID = "66666666-6666-4666-8666-666666666666"
MESSAGE_ID = "99999999-9999-4999-8999-999999999999"
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


@dataclass
class FakeAssistant:
    sessions: list[AssistantSession] = field(default_factory=lambda: list[AssistantSession]())
    session: AssistantSession | None = None
    messages: list[AssistantMessage] = field(default_factory=lambda: list[AssistantMessage]())
    calls: list[tuple[object, ...]] = field(default_factory=lambda: list[tuple[object, ...]]())

    async def list_sessions(self, workspace_id: str) -> list[AssistantSession]:
        self.calls.append(("list", workspace_id))
        return self.sessions

    async def get_conversation(
        self, workspace_id: str, session_id: str
    ) -> tuple[AssistantSession, list[AssistantMessage]]:
        self.calls.append(("get", workspace_id, session_id))
        if self.session is None:
            raise SessionNotFoundError
        return self.session, self.messages


def app(repository: FakeAssistant | None):
    return create_app(
        load_settings({"CORS_ALLOWED_ORIGINS": "https://app.example.com"}),
        dependencies=AppDependencies(identity_adapter=identity_adapter(), assistant=repository),
    )


def session() -> AssistantSession:
    return AssistantSession(
        id=SESSION_ID,
        title="Policy review",
        web_search_enabled=True,
        last_message_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def message() -> AssistantMessage:
    return AssistantMessage(
        id=MESSAGE_ID,
        role="assistant",
        kind="text",
        payload={"content": "Done"},
        sequence_no=2,
        created_at=NOW,
    )


@pytest.mark.anyio
async def test_session_list_is_workspace_scoped_and_empty_array() -> None:
    repository = FakeAssistant(sessions=[])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        path = "/api/assistant/sessions"
        response = await client.get(path, headers=signed_headers("GET", path, WORKSPACE_ID))

    assert response.status_code == 200
    assert response.json() == {"sessions": []}
    assert repository.calls == [("list", WORKSPACE_ID)]


@pytest.mark.anyio
async def test_session_detail_preserves_message_payload_object() -> None:
    repository = FakeAssistant(session=session(), messages=[message()])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        path = f"/api/assistant/sessions/{SESSION_ID}"
        response = await client.get(path, headers=signed_headers("GET", path, WORKSPACE_ID))

    assert response.status_code == 200
    assert response.json() == {
        "session": {
            "id": SESSION_ID,
            "title": "Policy review",
            "web_search_enabled": True,
            "last_message_at": "2026-08-12T12:00:00Z",
            "created_at": "2026-08-12T12:00:00Z",
            "updated_at": "2026-08-12T12:00:00Z",
        },
        "messages": [
            {
                "id": MESSAGE_ID,
                "role": "assistant",
                "kind": "text",
                "payload": {"content": "Done"},
                "sequence_no": 2,
                "created_at": "2026-08-12T12:00:00Z",
            }
        ],
    }
    assert repository.calls == [("get", WORKSPACE_ID, SESSION_ID)]


@pytest.mark.anyio
async def test_session_uuid_and_missing_contract() -> None:
    repository = FakeAssistant()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        invalid_path = "/api/assistant/sessions/not-a-uuid"
        missing_path = f"/api/assistant/sessions/{SESSION_ID}"
        invalid = await client.get(
            invalid_path, headers=signed_headers("GET", invalid_path, WORKSPACE_ID)
        )
        missing = await client.get(
            missing_path, headers=signed_headers("GET", missing_path, WORKSPACE_ID)
        )

    assert invalid.status_code == 400
    assert invalid.json() == {"error": "会话 ID 非法"}
    assert missing.status_code == 404
    assert missing.json() == {"error": "会话不存在"}


@pytest.mark.anyio
async def test_session_queries_require_trusted_identity_before_repository_access() -> None:
    repository = FakeAssistant(sessions=[session()])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        response = await client.get("/api/assistant/sessions")

    assert response.status_code == 401
    assert response.json() == {"error": "durable identity is required"}
    assert repository.calls == []


@pytest.mark.anyio
async def test_session_detail_does_not_reveal_another_workspace_object() -> None:
    class WorkspaceAssistant(FakeAssistant):
        async def get_conversation(
            self, workspace_id: str, session_id: str
        ) -> tuple[AssistantSession, list[AssistantMessage]]:
            self.calls.append(("get", workspace_id, session_id))
            if workspace_id != WORKSPACE_ID or self.session is None:
                raise SessionNotFoundError
            return self.session, self.messages

    repository = WorkspaceAssistant(session=session())
    path = f"/api/assistant/sessions/{SESSION_ID}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(repository)), base_url="http://test"
    ) as client:
        response = await client.get(
            path, headers=signed_headers("GET", path, OTHER_WORKSPACE_ID)
        )

    assert response.status_code == 404
    assert response.json() == {"error": "会话不存在"}
    assert repository.calls == [("get", OTHER_WORKSPACE_ID, SESSION_ID)]
