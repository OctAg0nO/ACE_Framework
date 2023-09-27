from base.settings import Settings


settings = Settings(
    role_name="layer_6_prosecutor",
    send_hello=False,
    northbound_subscribe_queue="northbound.layer_6_prosecutor",
    southbound_subscribe_queue="southbound.layer_6_prosecutor",
    southbound_publish_queue="deadletter",
    northbound_publish_queue="northbound.layer_5_controller",
)