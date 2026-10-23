/*
 * Defense in depth for research egress. The sandbox network namespace is the
 * boundary: it has no route to the host or the internet, so UDP, DNS, and raw
 * sockets cannot leave. This library is loaded inside that namespace and also
 * refuses the same bypasses if a process is dynamically linked:
 *
 *   - TCP connects to anything except the loopback proxy port are rewritten to
 *     that proxy (HTTP CONNECT) or refused when they are loopback-but-not-proxy.
 *   - UDP / raw send, sendto, sendmsg, and sendmmsg fail with EACCES.
 *   - AF_UNIX is left alone so the in-sandbox forwarder can reach the host proxy.
 *
 * The proxy port comes from CAIRN_EGRESS_PROXY=127.0.0.1:PORT. The host is
 * ignored; the proxy is always loopback.
 *
 * Compile: cc -shared -fPIC -O2 -o libcairn_egress.so egress_preload.c -ldl
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
static ssize_t (*real_send)(int, const void *, size_t, int) = NULL;
static ssize_t (*real_sendto)(int, const void *, size_t, int, const struct sockaddr *, socklen_t) = NULL;
static ssize_t (*real_sendmsg)(int, const struct msghdr *, int) = NULL;
static int (*real_sendmmsg)(int, struct mmsghdr *, unsigned int, int) = NULL;
static int proxy_ready = 0;
static in_port_t proxy_port = 0;

static void resolve_proxy(void) {
    const char *p = getenv("CAIRN_EGRESS_PROXY");
    proxy_port = 0;
    if (!p) return;
    const char *colon = strrchr(p, ':');
    if (!colon) return;
    int port = atoi(colon + 1);
    if (port <= 0 || port > 65535) return;
    proxy_port = (in_port_t)port;
}

static int proxy_configured(void) {
    if (!real_connect) real_connect = dlsym(RTLD_NEXT, "connect");
    if (!proxy_ready) { proxy_ready = 1; resolve_proxy(); }
    return proxy_port != 0;
}

static int is_ip(const struct sockaddr *sa) {
    return sa && (sa->sa_family == AF_INET || sa->sa_family == AF_INET6);
}

static int is_loopback_ip(const struct sockaddr *sa) {
    if (!sa) return 0;
    if (sa->sa_family == AF_INET) {
        const struct sockaddr_in *sin = (const struct sockaddr_in *)sa;
        return (ntohl(sin->sin_addr.s_addr) >> 24) == 127;
    }
    if (sa->sa_family == AF_INET6) {
        const struct sockaddr_in6 *sin6 = (const struct sockaddr_in6 *)sa;
        return IN6_IS_ADDR_LOOPBACK(&sin6->sin6_addr);
    }
    return 0;
}

static int is_proxy_endpoint(const struct sockaddr *sa) {
    if (!is_loopback_ip(sa) || proxy_port == 0) return 0;
    if (sa->sa_family == AF_INET)
        return ((const struct sockaddr_in *)sa)->sin_port == htons(proxy_port);
    if (sa->sa_family == AF_INET6)
        return ((const struct sockaddr_in6 *)sa)->sin6_port == htons(proxy_port);
    return 0;
}

static int is_datagram(int fd) {
    int type = 0;
    socklen_t n = sizeof type;
    if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &n) != 0) return 0;
    return type == SOCK_DGRAM || type == SOCK_RAW;
}

static int refuse_datagram(int fd) {
    if (!proxy_configured() || !is_datagram(fd)) return 0;
    errno = EACCES;
    return 1;
}

static void send_connect_preamble(int fd, const char *host, int port) {
    char req[512];
    int n = snprintf(req, sizeof req,
                     "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n",
                     host, port, host, port);
    if (n < 0) return;
    if (!real_send) real_send = dlsym(RTLD_NEXT, "send");
    (void)real_send(fd, req, (size_t)n, MSG_NOSIGNAL);
}

static int connect_proxy_v4(int fd) {
    struct sockaddr_in proxy;
    memset(&proxy, 0, sizeof proxy);
    proxy.sin_family = AF_INET;
    proxy.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    proxy.sin_port = htons(proxy_port);
    return real_connect(fd, (const struct sockaddr *)&proxy, sizeof proxy);
}

int connect(int fd, const struct sockaddr *addr, socklen_t len) {
    if (!proxy_configured() || addr == NULL)
        return real_connect(fd, addr, len);
    if (is_datagram(fd)) {
        errno = EACCES;
        return -1;
    }
    if (!is_ip(addr))
        return real_connect(fd, addr, len);
    if (is_loopback_ip(addr)) {
        if (is_proxy_endpoint(addr))
            return real_connect(fd, addr, len);
        errno = ECONNREFUSED;
        return -1;
    }

    if (addr->sa_family == AF_INET) {
        const struct sockaddr_in *sin = (const struct sockaddr_in *)addr;
        char host[INET_ADDRSTRLEN];
        inet_ntop(AF_INET, &sin->sin_addr, host, sizeof host);
        int r = connect_proxy_v4(fd);
        if (r != 0) return r;
        send_connect_preamble(fd, host, ntohs(sin->sin_port));
        return 0;
    }
    if (addr->sa_family == AF_INET6) {
        const struct sockaddr_in6 *sin6 = (const struct sockaddr_in6 *)addr;
        char host[INET6_ADDRSTRLEN];
        char hostb[96];
        inet_ntop(AF_INET6, &sin6->sin6_addr, host, sizeof host);
        int r = connect_proxy_v4(fd);
        if (r != 0) return r;
        snprintf(hostb, sizeof hostb, "[%s]", host);
        send_connect_preamble(fd, hostb, ntohs(sin6->sin6_port));
        return 0;
    }
    return real_connect(fd, addr, len);
}

ssize_t send(int fd, const void *buf, size_t len, int flags) {
    if (!real_send) real_send = dlsym(RTLD_NEXT, "send");
    if (refuse_datagram(fd)) return -1;
    return real_send(fd, buf, len, flags);
}

ssize_t sendto(int fd, const void *buf, size_t len, int flags,
               const struct sockaddr *addr, socklen_t addrlen) {
    if (!real_sendto) real_sendto = dlsym(RTLD_NEXT, "sendto");
    if (refuse_datagram(fd)) return -1;
    return real_sendto(fd, buf, len, flags, addr, addrlen);
}

ssize_t sendmsg(int fd, const struct msghdr *msg, int flags) {
    if (!real_sendmsg) real_sendmsg = dlsym(RTLD_NEXT, "sendmsg");
    if (refuse_datagram(fd)) return -1;
    return real_sendmsg(fd, msg, flags);
}

int sendmmsg(int fd, struct mmsghdr *msgvec, unsigned int vlen, int flags) {
    if (!real_sendmmsg) real_sendmmsg = dlsym(RTLD_NEXT, "sendmmsg");
    if (refuse_datagram(fd)) return -1;
    return real_sendmmsg(fd, msgvec, vlen, flags);
}
