option(WITH_CAMERA "Build with OV7670 camera support" OFF)

set(priv_requires "")
if(WITH_CAMERA)
    list(APPEND priv_requires esp32-camera)
endif()

idf_component_register(SRCS "main.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES ${priv_requires}
                       EMBED_TXTFILES "server_root_ca.pem")

if(WITH_CAMERA)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE WITH_CAMERA=1)
endif()
