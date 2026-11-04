FROM busybox:1.37.0@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec

LABEL org.opencontainers.image.source="ewallet-data-ai-platform" \
      org.opencontainers.image.title="EDAI-2 immutable Jenkins source snapshot"

COPY --chown=1000:1000 repository.bundle source-metadata.txt /snapshot/

USER 1000:1000
ENTRYPOINT ["/bin/sh", "-ec"]
