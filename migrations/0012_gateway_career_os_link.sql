-- 0012_gateway_career_os_link: the gateway may check that an account has an active Career-OS
-- link, for stages that require one before a session starts (`COPILOT_REQUIRE_CAREER_OS`).
-- Source: ARCHITECTURE.md, "Career-OS link".
--
-- A column grant: the link's owner, provider and status only, never the sealed token.
-- Expand only: one grant.

set local role app_owner;

grant select (user_id, provider, status) on app.integrations to app_gateway;
