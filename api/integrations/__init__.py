"""Server-to-server integration layer between the Domi Messager and
Domiitulua ("ops").

Modules:

- ``phones``    — phone normalization between WhatsApp and ops formats.
- ``services``  — ops ↔ calculator service type mapping.
- ``auth``      — ``IntegrationApiKey`` authentication, scopes, throttle.
- ``hmac``      — webhook signature verification (HMAC-SHA256 + timestamp).
- ``ops``       — HTTP client for the ops public API v1.
- ``llm``       — DeepSeek OpenAI-compatible client (order draft).
- ``notify``    — outbound message helpers (text/template, immediate send).
- ``views``     — ``/api/integrations/…`` endpoints.
"""
