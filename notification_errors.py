"""Delivery outcomes that are safe to retry without duplicating a message."""


class NotificationRejected(RuntimeError):
    """No message was accepted; unlike an unknown response, retry is safe."""
