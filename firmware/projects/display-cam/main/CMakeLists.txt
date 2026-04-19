idf_component_register(SRCS "main.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp32-camera
                       EMBED_TXTFILES "server_root_ca.pem")
