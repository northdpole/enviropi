from __future__ import annotations

import json
import logging
import ssl
from typing import Any

from paho.mqtt.client import CallbackAPIVersion, Client, MQTT_ERR_SUCCESS

from enviropi.config import EnvSettings

logger = logging.getLogger(__name__)

CLIENT_ID = "enviropi-collector"

PAYLOAD_KEYS = (
    "ts",
    "temperature",
    "humidity",
    "pressure",
    "lux",
    "noise",
    "gas_reducing",
    "gas_oxidising",
    "gas_nh3",
)


def encode_payload(sample: dict[str, Any]) -> str:
    """JSON body published to MQTT. Keys match Sample.as_dict()."""
    payload = {key: sample.get(key) for key in PAYLOAD_KEYS}
    return json.dumps(payload, allow_nan=False)


class MqttPublisher:
    """Publish-only MQTT client. Failures are logged; they never raise to the collector."""

    def __init__(self, env: EnvSettings) -> None:
        self._enabled = env.mqtt_enabled
        self._host = env.mqtt_host
        self._port = env.mqtt_port
        self._username = env.mqtt_username
        self._password = env.mqtt_password
        self._topic = env.mqtt_topic
        self._tls = env.mqtt_tls
        self._tls_insecure = env.mqtt_tls_insecure
        self._client: Client | None = None

        if not self._enabled:
            return
        if not self._username or not self._password:
            logger.error(
                "MQTT_ENABLED but MQTT_USERNAME/MQTT_PASSWORD empty; not connecting"
            )
            return
        self._ensure_client()

    def publish(self, sample: dict[str, Any]) -> None:
        if not self._enabled:
            return
        if not self._username or not self._password:
            return
        try:
            self._ensure_client()
            if self._client is None:
                return
            payload = encode_payload(sample)
            info = self._client.publish(self._topic, payload, qos=1, retain=True)
            if info.rc != MQTT_ERR_SUCCESS:
                logger.warning("MQTT publish rc=%s (broker will retry)", info.rc)
        except Exception:
            logger.exception("MQTT publish failed")

    def stop(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            client.disconnect()
            client.loop_stop()
        except Exception:
            logger.exception("MQTT stop failed")

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        try:
            self._client = self._make_client()
        except Exception:
            logger.exception(
                "MQTT client setup failed; will retry on the next sample"
            )
            self._client = None

    def _make_client(self) -> Client:
        client = Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=CLIENT_ID,
        )
        client.username_pw_set(self._username, self._password)
        client.reconnect_delay_set(min_delay=1, max_delay=120)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        if self._tls:
            # System CA bundle; Let's Encrypt / Tailscale server cert.
            # Clients must use the full hostname so the cert SAN matches.
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
            client.tls_insecure_set(self._tls_insecure)
        client.connect_async(self._host, self._port, keepalive=60)
        client.loop_start()
        return client

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties) -> None:
        if reason_code != 0:
            logger.warning("MQTT connect refused: %s", reason_code)
            return
        logger.info(
            "MQTT connected to %s:%s topic=%s",
            self._host,
            self._port,
            self._topic,
        )

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        if reason_code != 0:
            logger.warning("MQTT disconnected: %s (will reconnect)", reason_code)
