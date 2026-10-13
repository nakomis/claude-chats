idf_component_register(
    SRCS "full.c" "net.c" "vcore.c" "prov.c" "fleet.c" "ota.c" "kata_worker.c" "../../../tok/tok.c" "../../../otacmd/ota_cmd.c"
         # CHATS-5: the worker's portable modules (host-tested in ../../../test)
         "../../../worker/kw.c" "../../../worker/sigv4.c" "../../../worker/age.c" "../../../worker/prep.c"
         "../../../worker/result.c" "../../../worker/sqs.c" "../../../worker/creds.c" "../../../worker/worker.c"
         "../../../worker/batch.c"
    INCLUDE_DIRS "." "../../../tok" "../../../otacmd" "../../../worker"
    PRIV_REQUIRES esp_hw_support esp_timer esp_driver_sdmmc sdmmc fatfs esp_rom esp_wifi esp_netif esp_event
                  esp_http_client mbedtls nvs_flash esp_app_format app_update mqtt json esp_driver_tsens lwip
    # Amazon Root CA 1 (public): IoT Core, the credentials provider and the S3 download (CHATS-10). The
    # board's own certificate, key and settings are not in the image: they're in the prov partition
    # (prov.h, provision.sh).
    EMBED_TXTFILES "../AmazonRootCA1.pem")

# CHATS-5: cJSON recurses once per nesting level, and the worker's stack is app_main's 16 KB. Real
# envelopes and jobs nest three deep; the worker refuses anything over 32 before parsing it
# (kw_json_depth_ok), and cJSON itself stops there too.
idf_component_get_property(json_lib json COMPONENT_LIB)
target_compile_definitions(${json_lib} PRIVATE CJSON_NESTING_LIMIT=32)
