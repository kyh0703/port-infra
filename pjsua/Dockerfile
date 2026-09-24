FROM alpine:3.23 AS build
RUN apk add --no-cache build-base linux-headers openssl-dev curl
WORKDIR /build
RUN curl -fsSL https://github.com/pjsip/pjproject/archive/refs/tags/2.17.tar.gz -o pjproject.tar.gz \
    && echo '065fe06c06788d97c35f563796d59f00ce52fe9558a52d7b490a042a966facce  pjproject.tar.gz' | sha256sum -c - \
    && tar -xzf pjproject.tar.gz --strip-components=1
RUN ./configure --disable-video --disable-sound --disable-pjsua2 \
    && make dep \
    && make -j2 \
    && cp pjsip-apps/bin/pjsua-* /usr/local/bin/pjsua

FROM alpine:3.23
RUN apk add --no-cache libstdc++ libssl3 libcrypto3
COPY --from=build /usr/local/bin/pjsua /usr/local/bin/pjsua
ENTRYPOINT ["pjsua"]
