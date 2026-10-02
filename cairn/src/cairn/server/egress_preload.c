/*
 * cairn egress preload: force every outbound TCP connect through the local
 * loopback enforcement proxy so a research model cannot bypass egress control
 * by connecting directly to a target (skipping HTTP(S)_PROXY/ALL_PROXY env).
 *
 * Non-loopback AF_INET / AF_INET6 connects are rewritten to the loopback proxy
 * and an HTTP "CONNECT host:port" preamble is injected, so the proxy performs its
 * scope / quota / protocol authorization before the tunnel is established.
 * Loopback connections (127.0.0.1, ::1) are left untouched so the proxy itself
 * and local fixtures keep working.
 *
 * Compile: cc -shared -fPIC -O2 -o libcairn_egress.so egress_preload.c -ldl
 * Enable:  LD_PRELOAD=/path/libcairn_egress.so CAIRN_EGRESS_PROXY=127.0.0.1:PORT
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <dlfcn.h>

static int (*real_connect)(int, const struct sockaddr *, socklen_t) = NULL;
static int proxy_ready = 0;
static in_port_t proxy_port = 0;

static void resolve_proxy(void) {
    const char *p = getenv("CAIRN_EGRESS_PROXY");
    proxy_port = 0;
    if (!p) return;
    /* proxy is always the loopback host; only the port is taken from env */
    const char *colon = strrchr(p, ':');
    if (!colon) return;
    int port = atoi(colon + 1);
    if (port <= 0 || port > 65535) return;
    proxy_port = (in_port_t)port;
}

static int is_loopback(const struct sockaddr *sa) {
    if (!sa) return 1;
    if (sa->sa_family == AF_INET) {
        const struct sockaddr_in *sin = (const struct sockaddr_in *)sa;
        return ((ntohl(sin->sin_addr.s_addr) >> 24) == 127);
    }
    if (sa->sa_family == AF_INET6) {
        const struct sockaddr_in6 *sin6 = (const struct sockaddr_in6 *)sa;
        return IN6_IS_ADDR_LOOPBACK(&sin6->sin6_addr);
    }
    return 1; /* AF_UNIX etc. are not intercepted */
}

static void send_connect_preamble(int fd, const char *host, int port) {
    char req[512];
    int n = snprintf(req, sizeof req,
                     "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n",
                     host, port, host, port);
    if (n < 0) return;
    (void)send(fd, req, (size_t)n, MSG_NOSIGNAL);
}

int connect(int fd, const struct sockaddr *addr, socklen_t len) {
    if (!real_connect) real_connect = dlsym(RTLD_NEXT, "connect");
    if (!proxy_ready) { proxy_ready = 1; resolve_proxy(); }
    if (proxy_port == 0 || is_loopback(addr) || addr == NULL) {
        return real_connect(fd, addr, len);
    }

    if (addr->sa_family == AF_INET) {
        const struct sockaddr_in *sin = (const struct sockaddr_in *)addr;
        char host[INET_ADDRSTRLEN];
        inet_ntop(AF_INET, &sin->sin_addr, host, sizeof host);
        struct sockaddr_in proxy;
        memset(&proxy, 0, sizeof proxy);
        proxy.sin_family = AF_INET;
        proxy.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        proxy.sin_port = htons(proxy_port);
        int r = real_connect(fd, (const struct sockaddr *)&proxy, sizeof proxy);
        if (r != 0) return r;
        send_connect_preamble(fd, host, ntohs(sin->sin_port));
        return 0;
    }
    if (addr->sa_family == AF_INET6) {
        const struct sockaddr_in6 *sin6 = (const struct sockaddr_in6 *)addr;
        char host[INET6_ADDRSTRLEN];
        inet_ntop(AF_INET6, &sin6->sin6_addr, host, sizeof host);
        struct sockaddr_in6 proxy;
        memset(&proxy, 0, sizeof proxy);
        proxy.sin6_family = AF_INET6;
        proxy.sin6_addr = in6addr_loopback;
        proxy.sin6_port = htons(proxy_port);
        int r = real_connect(fd, (const struct sockaddr *)&proxy, sizeof proxy);
        if (r != 0) return r;
        int port = ntohs(sin6->sin6_port);
        char hostb[96];
        snprintf(hostb, sizeof hostb, "[%s]", host);
        send_connect_preamble(fd, hostb, port);
        return 0;
    }
    return real_connect(fd, addr, len);
}