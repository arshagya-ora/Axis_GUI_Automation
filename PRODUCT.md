# ORACLE AXIS

AXIS is a local browser assistant for the Chrome session the user already uses. A Python planner and navigator operate signed-in tabs through a Manifest V3 extension and native browser bridge. The side panel is a conversation workspace: describe a task, follow its progress, review changes when required, and read the outcome.

## Users and operating context

People doing browser-based research, document work, and operational workflows use AXIS alongside their normal desktop browser. The primary surface is a narrow, resizable Chrome side panel; settings also open in a full browser tab. Light, dark, and system appearance support different working environments.

## Product requirements

- Preserve task ownership, explicit execution modes, action approvals, pause/resume/stop, clarification, recovery, attachments, local history, and saved outcomes.
- Service and browser bridge connections are separate. Never imply that the browser is connected when either is unavailable.
- Pending submissions retain their original request ID and target. A newer draft must survive retry and reconnection.
- Browser and document contents are untrusted. Render them as text or the existing safe Markdown subset.
- Keep configuration and credentials local; never add remote fonts, telemetry, or runtime design dependencies.
- Use plain action labels and explain the next available step when work is blocked.

## Confirmed design preference

The user selected a conversation-first layout with an Oracle Redwood visual identity. Keep every capability, revealing technical controls on demand. Use the ORACLE AXIS name, the user-supplied Oracle PNG logo, and locally bundled Oracle Sans for the displayed brand name. Implement with the existing packaged JavaScript and CSS, without migrating to Oracle JET.
