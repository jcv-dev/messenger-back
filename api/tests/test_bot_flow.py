"""Tests for the bot state machine — tests handlers via advance()."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TestCase

from api.bot.flow import (
    WELCOME,
    AWAITING_PROFILE,
    AWAITING_SERVICE_TYPE,
    AWAITING_ORIGIN,
    CONFIRMING_ORIGIN,
    AWAITING_DESTINATION,
    CONFIRMING_DEST,
    AWAITING_SEGMENT_DESCRIPTION,
    AWAITING_SEGMENT_INSTRUCTIONS,
    AWAITING_MORE_STOPS,
    AWAITING_TOOLS,
    AWAITING_PAYMENT,
    AWAITING_ACOMPANANTE,
    CONFIRMING_QUOTE,
    AWAITING_RECIPIENT_NAME,
    AWAITING_RECIPIENT_PHONE,
    ASK_BANCARIOS_ENTITY,
    ASK_BANCARIOS_REFERENCE,
    ASK_KNOWS_RECIPIENT,
    advance,
    build_initial_session,
)

_NON_DIGIT = re.compile(r"\D")


def _strip_phone(text: str) -> str:
    return _NON_DIGIT.sub("", text) if text else ""


class BotFlowTests(TestCase):
    """Test the bot state machine handlers with mocked external deps."""

    GEO_RESULT = [{"place_id": "ChIJtest123", "display_name": "Calle Test, Tuluá"}]
    GEO_DETAILS = {"lat": 4.123, "lng": -76.456, "display_name": "Calle Test, Tuluá"}
    PRICE_RESULT = {"breakdown": {"items": [], "total": 10000}}

    @patch("api.bot.flow._llm_classify_intent", new_callable=AsyncMock)
    @patch("api.bot.flow._send_location", new_callable=AsyncMock)
    @patch("api.bot.flow.calculator.geocode_search", new_callable=AsyncMock)
    @patch("api.bot.flow.calculator.geocode_details", new_callable=AsyncMock)
    @patch("api.bot.flow.calculator.calculate_price", new_callable=AsyncMock)
    @patch("api.bot.flow.calculator.get_tools", new_callable=AsyncMock)
    @patch("api.bot.flow.get_bot_user_async", new_callable=AsyncMock)
    async def test_domicilios_happy_path(
        self,
        mock_bot_user,
        mock_get_tools,
        mock_calc_price,
        mock_geo_details,
        mock_geo_search,
        mock_send_loc,
        mock_llm,
    ):
        mock_geo_search.return_value = self.GEO_RESULT
        mock_geo_details.return_value = self.GEO_DETAILS
        mock_calc_price.return_value = self.PRICE_RESULT
        mock_get_tools.return_value = []

        conv = MagicMock(id=12345, contact_phone="+573001234567")
        session = build_initial_session()

        # 1. WELCOME -> Cotizar domicilio -> AWAITING_PROFILE
        r = await advance(conv, session, "Cotizar domicilio", button_id="cotizar")
        self.assertEqual(r.state, AWAITING_PROFILE)
        self.assertFalse(r.fallback)
        self.assertIsNotNone(r.send_interactive)
        self.assertIsNone(session.get("data", {}).get("collected", {}).get("profile"))

        # 2. AWAITING_PROFILE -> Usuario final -> AWAITING_SERVICE_TYPE
        r = await advance(conv, session, "Usuario final", button_id="final")
        self.assertEqual(r.state, AWAITING_SERVICE_TYPE)
        self.assertEqual(session["data"]["collected"]["profile"], "usuario_final")

        # 3. AWAITING_SERVICE_TYPE -> Domicilios -> AWAITING_ORIGIN
        r = await advance(conv, session, "Domicilios", button_id="domicilios")
        self.assertEqual(r.state, AWAITING_ORIGIN)
        self.assertEqual(session["data"]["collected"]["service_type"], "domicilios")
        self.assertFalse(session["data"]["collected"].get("coords_optional", False))

        # 4. AWAITING_ORIGIN -> write address -> CONFIRMING_ORIGIN
        r = await advance(conv, session, "Cra 1 #2-3, Tuluá")
        self.assertEqual(r.state, CONFIRMING_ORIGIN)
        seg = session["data"]["collected"]["segments"][0]
        self.assertEqual(seg["origin"]["address"], "Calle Test, Tuluá")
        self.assertEqual(seg["origin"]["lat"], 4.123)
        self.assertFalse(seg["origin"]["confirmed"])
        mock_geo_search.assert_called_once()
        mock_geo_details.assert_called_once()
        mock_send_loc.assert_called_once()

        # 5. CONFIRMING_ORIGIN -> confirm yes -> AWAITING_DESTINATION
        r = await advance(conv, session, "Sí", button_id="yes")
        self.assertEqual(r.state, AWAITING_DESTINATION)
        self.assertTrue(seg["origin"]["confirmed"])

        # 6. AWAITING_DESTINATION -> write address -> CONFIRMING_DEST
        r = await advance(conv, session, "Cra 5 #10-20, Tuluá")
        self.assertEqual(r.state, CONFIRMING_DEST)
        seg = session["data"]["collected"]["segments"][0]
        self.assertEqual(seg["destination"]["address"], "Calle Test, Tuluá")
        self.assertFalse(seg["destination"]["confirmed"])

        # 7. CONFIRMING_DEST -> confirm yes -> AWAITING_SEGMENT_DESCRIPTION
        r = await advance(conv, session, "Sí", button_id="yes")
        self.assertEqual(r.state, AWAITING_SEGMENT_DESCRIPTION)
        self.assertTrue(seg["destination"]["confirmed"])

        # 8. AWAITING_SEGMENT_DESCRIPTION -> describe package -> AWAITING_SEGMENT_INSTRUCTIONS
        r = await advance(conv, session, "Un paquete de ropa")
        self.assertEqual(r.state, AWAITING_SEGMENT_INSTRUCTIONS)
        self.assertEqual(seg["description"], "Un paquete de ropa")

        # 9. AWAITING_SEGMENT_INSTRUCTIONS -> no instructions -> AWAITING_MORE_STOPS
        r = await advance(conv, session, "no")
        self.assertEqual(r.state, AWAITING_MORE_STOPS)
        self.assertIsNone(seg["instructions"])

        # 10. AWAITING_MORE_STOPS -> no more stops -> AWAITING_TOOLS
        r = await advance(conv, session, "No", button_id="no")
        self.assertEqual(r.state, AWAITING_TOOLS)
        self.assertIsNotNone(r.send_interactive)

        # 11. AWAITING_TOOLS -> no tools -> AWAITING_PAYMENT
        r = await advance(conv, session, "Ninguna", button_id="none")
        self.assertEqual(r.state, AWAITING_PAYMENT)
        self.assertEqual(session["data"]["collected"]["tool_keys"], [])

        # 12. AWAITING_PAYMENT -> efectivo -> AWAITING_ACOMPANANTE
        r = await advance(conv, session, "Efectivo", button_id="efectivo")
        self.assertEqual(r.state, AWAITING_ACOMPANANTE)
        self.assertEqual(session["data"]["collected"]["payment_method"], "efectivo")

        # 13. AWAITING_ACOMPANANTE -> no acompañante -> CONFIRMING_QUOTE
        r = await advance(conv, session, "No", button_id="no")
        self.assertEqual(r.state, CONFIRMING_QUOTE)
        self.assertFalse(session["data"]["collected"]["acompanante"])
        self.assertIn("Total", r.messages[0]["content"])
        mock_calc_price.assert_called_once()

        # 14. CONFIRMING_QUOTE -> confirm -> ASK_KNOWS_RECIPIENT
        r = await advance(conv, session, "Confirmar", button_id="confirm")
        self.assertEqual(r.state, ASK_KNOWS_RECIPIENT)

        # 15. ASK_KNOWS_RECIPIENT -> yes -> AWAITING_RECIPIENT_NAME
        r = await advance(conv, session, "Sí", button_id="yes")
        self.assertEqual(r.state, AWAITING_RECIPIENT_NAME)

        # 17. AWAITING_RECIPIENT_NAME -> type name -> AWAITING_RECIPIENT_PHONE
        r = await advance(conv, session, "Juan Pérez")
        self.assertEqual(r.state, AWAITING_RECIPIENT_PHONE)
        self.assertEqual(session["data"]["collected"]["recipient_name"], "Juan Pérez")

        # 18. AWAITING_RECIPIENT_PHONE -> type phone -> WELCOME (order submitted)
        r = await advance(conv, session, "3151234567")
        self.assertEqual(r.state, WELCOME)
        self.assertIn("enviado exitosamente", r.messages[0]["content"])
        self.assertIsNone(r.send_interactive)
        self.assertEqual(
            session["data"]["collected"]["recipient_phone"],
            _strip_phone("3151234567"),
        )

    @patch("api.bot.flow._llm_classify_intent", new_callable=AsyncMock)
    @patch("api.bot.flow._send_location", new_callable=AsyncMock)
    @patch("api.bot.flow.calculator.geocode_search", new_callable=AsyncMock)
    @patch("api.bot.flow.calculator.geocode_details", new_callable=AsyncMock)
    @patch("api.bot.flow.calculator.calculate_price", new_callable=AsyncMock)
    @patch("api.bot.flow.calculator.get_tools", new_callable=AsyncMock)
    @patch("api.bot.flow.get_bot_user_async", new_callable=AsyncMock)
    async def test_purchases_flow(
        self,
        mock_bot_user,
        mock_get_tools,
        mock_calc_price,
        mock_geo_details,
        mock_geo_search,
        mock_send_loc,
        mock_llm,
    ):
        mock_geo_search.return_value = self.GEO_RESULT
        mock_geo_details.return_value = self.GEO_DETAILS
        mock_calc_price.return_value = self.PRICE_RESULT
        mock_get_tools.return_value = []

        conv = MagicMock(id=12346, contact_phone="+573001234568")
        session = build_initial_session()

        # WELCOME -> Cotizar -> AWAITING_PROFILE
        r = await advance(conv, session, "Cotizar", button_id="cotizar")
        self.assertEqual(r.state, AWAITING_PROFILE)

        # AWAITING_PROFILE -> final -> AWAITING_SERVICE_TYPE
        r = await advance(conv, session, "final", button_id="final")
        self.assertEqual(r.state, AWAITING_SERVICE_TYPE)

        # AWAITING_SERVICE_TYPE -> purchases -> AWAITING_ORIGIN (coords optional)
        r = await advance(conv, session, "Compras", button_id="purchases")
        self.assertEqual(r.state, AWAITING_ORIGIN)
        self.assertTrue(session["data"]["collected"]["coords_optional"])
        self.assertIn("Opcional", r.messages[0]["content"])

        # AWAITING_ORIGIN -> skip (no) -> AWAITING_DESTINATION (optional)
        r = await advance(conv, session, "no")
        self.assertEqual(r.state, AWAITING_DESTINATION)
        seg = session["data"]["collected"]["segments"][0]
        self.assertIsNone(seg["origin"]["address"])
        self.assertIn("Opcional", r.messages[0]["content"])

        # AWAITING_DESTINATION -> skip (omitir) -> AWAITING_SEGMENT_DESCRIPTION
        r = await advance(conv, session, "omitir")
        self.assertEqual(r.state, AWAITING_SEGMENT_DESCRIPTION)
        seg = session["data"]["collected"]["segments"][0]
        self.assertIsNone(seg["destination"]["address"])
        self.assertIn("compre", r.messages[0]["content"])

        # AWAITING_SEGMENT_DESCRIPTION -> description
        r = await advance(conv, session, "Un mercado")
        self.assertEqual(r.state, AWAITING_SEGMENT_INSTRUCTIONS)
        self.assertEqual(seg["description"], "Un mercado")

        # AWAITING_SEGMENT_INSTRUCTIONS -> no
        r = await advance(conv, session, "no")
        self.assertEqual(r.state, AWAITING_MORE_STOPS)

        # AWAITING_MORE_STOPS -> no
        r = await advance(conv, session, "No", button_id="no")
        self.assertEqual(r.state, AWAITING_TOOLS)

        # AWAITING_TOOLS -> none
        r = await advance(conv, session, "Ninguna", button_id="none")
        self.assertEqual(r.state, AWAITING_PAYMENT)

        # AWAITING_PAYMENT -> efectivo
        r = await advance(conv, session, "Efectivo", button_id="efectivo")
        self.assertEqual(r.state, AWAITING_ACOMPANANTE)

        # AWAITING_ACOMPANANTE -> no
        r = await advance(conv, session, "No", button_id="no")
        self.assertEqual(r.state, CONFIRMING_QUOTE)

        # CONFIRMING_QUOTE -> confirm
        r = await advance(conv, session, "Confirmar", button_id="confirm")
        self.assertEqual(r.state, ASK_KNOWS_RECIPIENT)

        # ASK_KNOWS_RECIPIENT -> yes -> AWAITING_RECIPIENT_NAME
        r = await advance(conv, session, "Sí", button_id="yes")
        self.assertEqual(r.state, AWAITING_RECIPIENT_NAME)

        # AWAITING_RECIPIENT_NAME -> name
        r = await advance(conv, session, "María Gómez")
        self.assertEqual(r.state, AWAITING_RECIPIENT_PHONE)

        # AWAITING_RECIPIENT_PHONE -> phone -> WELCOME
        r = await advance(conv, session, "3201234567")
        self.assertEqual(r.state, WELCOME)
        self.assertIn("enviado exitosamente", r.messages[0]["content"])
        self.assertIsNone(r.send_interactive)

    @patch("api.bot.flow._llm_classify_intent", new_callable=AsyncMock)
    @patch("api.bot.flow._send_location", new_callable=AsyncMock)
    @patch("api.bot.flow.calculator.geocode_search", new_callable=AsyncMock)
    @patch("api.bot.flow.calculator.geocode_details", new_callable=AsyncMock)
    @patch("api.bot.flow.calculator.calculate_price", new_callable=AsyncMock)
    @patch("api.bot.flow.calculator.get_tools", new_callable=AsyncMock)
    @patch("api.bot.flow.get_bot_user_async", new_callable=AsyncMock)
    async def test_bancarios_flow(
        self,
        mock_bot_user,
        mock_get_tools,
        mock_calc_price,
        mock_geo_details,
        mock_geo_search,
        mock_send_loc,
        mock_llm,
    ):
        mock_geo_search.return_value = self.GEO_RESULT
        mock_geo_details.return_value = self.GEO_DETAILS
        mock_calc_price.return_value = self.PRICE_RESULT
        mock_get_tools.return_value = []

        conv = MagicMock(id=12347, contact_phone="+573001234569")
        session = build_initial_session()

        # WELCOME -> Cotizar -> AWAITING_PROFILE
        r = await advance(conv, session, "Cotizar", button_id="cotizar")
        self.assertEqual(r.state, AWAITING_PROFILE)

        # AWAITING_PROFILE -> final -> AWAITING_SERVICE_TYPE
        r = await advance(conv, session, "final", button_id="final")
        self.assertEqual(r.state, AWAITING_SERVICE_TYPE)

        # AWAITING_SERVICE_TYPE -> bancarios -> AWAITING_ORIGIN (coords optional)
        r = await advance(conv, session, "Bancarios", button_id="bancarios")
        self.assertEqual(r.state, AWAITING_ORIGIN)
        self.assertTrue(session["data"]["collected"]["coords_optional"])

        # AWAITING_ORIGIN -> skip -> AWAITING_DESTINATION
        r = await advance(conv, session, "no")
        self.assertEqual(r.state, AWAITING_DESTINATION)

        # AWAITING_DESTINATION -> skip -> AWAITING_SEGMENT_DESCRIPTION
        r = await advance(conv, session, "no")
        self.assertEqual(r.state, AWAITING_SEGMENT_DESCRIPTION)
        self.assertIn("bancario", r.messages[0]["content"])

        # AWAITING_SEGMENT_DESCRIPTION -> description
        r = await advance(conv, session, "Pago de factura")
        self.assertEqual(r.state, AWAITING_SEGMENT_INSTRUCTIONS)
        seg = session["data"]["collected"]["segments"][0]
        self.assertEqual(seg["description"], "Pago de factura")

        # AWAITING_SEGMENT_INSTRUCTIONS -> no -> AWAITING_MORE_STOPS
        r = await advance(conv, session, "no")
        self.assertEqual(r.state, AWAITING_MORE_STOPS)

        # AWAITING_MORE_STOPS -> no -> ASK_BANCARIOS_ENTITY (bancarios-specific)
        r = await advance(conv, session, "No", button_id="no")
        self.assertEqual(r.state, ASK_BANCARIOS_ENTITY)

        # ASK_BANCARIOS_ENTITY -> entity -> ASK_BANCARIOS_REFERENCE
        r = await advance(conv, session, "Bancolombia")
        self.assertEqual(r.state, ASK_BANCARIOS_REFERENCE)
        self.assertEqual(session["data"]["collected"]["entity"], "Bancolombia")

        # ASK_BANCARIOS_REFERENCE -> reference -> AWAITING_TOOLS
        r = await advance(conv, session, "REF-12345")
        self.assertEqual(r.state, AWAITING_TOOLS)
        self.assertEqual(session["data"]["collected"]["reference"], "REF-12345")

        # Rest of flow same as normal -> AWAITING_PAYMENT
        r = await advance(conv, session, "Ninguna", button_id="none")
        self.assertEqual(r.state, AWAITING_PAYMENT)

        r = await advance(conv, session, "Efectivo", button_id="efectivo")
        self.assertEqual(r.state, AWAITING_ACOMPANANTE)

        r = await advance(conv, session, "No", button_id="no")
        self.assertEqual(r.state, CONFIRMING_QUOTE)

        r = await advance(conv, session, "Confirmar", button_id="confirm")
        self.assertEqual(r.state, ASK_KNOWS_RECIPIENT)

        r = await advance(conv, session, "Sí", button_id="yes")
        self.assertEqual(r.state, AWAITING_RECIPIENT_NAME)

        r = await advance(conv, session, "Carlos López")
        self.assertEqual(r.state, AWAITING_RECIPIENT_PHONE)

        r = await advance(conv, session, "3105551234")
        self.assertEqual(r.state, WELCOME)
        self.assertIn("enviado exitosamente", r.messages[0]["content"])
        self.assertIsNone(r.send_interactive)

    @patch("api.bot.flow._llm_classify_intent", new_callable=AsyncMock)
    @patch("api.bot.flow._send_location", new_callable=AsyncMock)
    @patch("api.bot.flow.calculator.geocode_search", new_callable=AsyncMock)
    @patch("api.bot.flow.calculator.geocode_details", new_callable=AsyncMock)
    @patch("api.bot.flow.calculator.calculate_price", new_callable=AsyncMock)
    @patch("api.bot.flow.calculator.get_tools", new_callable=AsyncMock)
    @patch("api.bot.flow.get_bot_user_async", new_callable=AsyncMock)
    async def test_greeting_at_welcome(
        self,
        mock_bot_user,
        mock_get_tools,
        mock_calc_price,
        mock_geo_details,
        mock_geo_search,
        mock_send_loc,
        mock_llm,
    ):
        conv = MagicMock(id=12348, contact_phone="+573001234570")
        session = build_initial_session()

        # Send "hola" — should match greeting regex, return welcome menu, no fallback
        r = await advance(conv, session, "hola")
        self.assertEqual(r.state, WELCOME)
        self.assertFalse(r.fallback)
        self.assertIsNotNone(r.send_interactive)
        self.assertEqual(len(r.messages), 0)

        # Various greetings should all work
        for greeting in ["Hola", "holaaa", "buenas", "qué tal", "hey", "hi", "hola buenas"]:
            session = build_initial_session()
            r = await advance(conv, session, greeting)
            self.assertEqual(r.state, WELCOME, f"greeting '{greeting}' failed")
            self.assertFalse(r.fallback, f"greeting '{greeting}' triggered fallback")

        # Non-greeting text should NOT match greeting regex
        # Goes to LLM (returns None) → fallback
        for not_greeting in ["domicilio", "quiero un domicilio"]:
            session = build_initial_session()
            mock_llm.return_value = None
            r = await advance(conv, session, not_greeting)
            self.assertTrue(r.fallback, f"'{not_greeting}' should be fallback")
            self.assertIsNotNone(r.send_interactive)

        # Confusion words ("me ayudas") → confusion handler, not fallback
        session = build_initial_session()
        r = await advance(conv, session, "me ayudas")
        self.assertIn("Con gusto", r.messages[0]["content"])
        self.assertFalse(r.fallback)
