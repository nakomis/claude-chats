idf_component_register(
    SRCS "full.c" "net.c" "vcore.c" "prov.c" "fleet.c" "ota.c" "../../../tok/tok.c" "../../../otacmd/ota_cmd.c"
    INCLUDE_DIRS "." "../../../tok" "../../../otacmd"
    PRIV_REQUIRES esp_hw_support esp_timer esp_driver_sdmmc sdmmc fatfs esp_rom esp_wifi esp_netif esp_event
                  esp_http_client mbedtls nvs_flash esp_app_format app_update mqtt json
    # Amazon Root CA 1 (public): IoT Core and the S3 download (CHATS-10). The board's own certificate,
    # key and settings are not in the image: they're in the prov partition (prov.h, provision.sh).
    EMBED_TXTFILES "../AmazonRootCA1.pem")
