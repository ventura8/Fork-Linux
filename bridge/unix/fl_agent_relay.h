/*
 * fl_agent_relay.h - the ssh-agent relay of fl-bridge-helper: lets Fork's bundled (msys)
 * ssh use the user's Linux ssh-agent without the git bridge.
 *
 * Fork's bundled Git for Windows runs its own msys OpenSSH. msys (a Cygwin fork) has no
 * real AF_UNIX sockets: a "socket" is a file with the Windows SYSTEM attribute holding
 * "!<socket >PORT s XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX", and connect() goes to
 * 127.0.0.1:PORT over TCP, then exchanges a handshake:
 *
 *   client -> secret[16]    the four hex words of the file, as little-endian u32s
 *   daemon -> secret[16]    the same bytes (the client checks them)
 *   client -> cred[12]      its pid, uid, gid (i32 LE each; ignored here)
 *   daemon -> cred[12]      ours
 *
 * after which the stream is the agent protocol. The daemon (--ssh-agent SOCK
 * --ssh-agent-file FILE) listens on a second loopback port, writes FILE (0600, in a 0700
 * directory of ours inside the prefix, SYSTEM attribute through Wine's "user.DOSATTRIB"
 * extended attribute) and forks one relay per connection. A relay accepts a peer only when
 * the TCP connection belongs to this user (/proc/net/tcp) and sends the secret of the file,
 * then forwards one agent request at a time to SOCK: listing keys, signing and the
 * session-bind extension pass; every other request (adding, removing or locking keys, ...)
 * is answered with SSH_AGENT_FAILURE and never reaches the agent.
 *
 * See bridge/unix/README.md ("ssh-agent relay") and docs/SECURITY.md.
 */
#ifndef FL_AGENT_RELAY_H
#define FL_AGENT_RELAY_H

#include <limits.h>
#include <netinet/in.h>
#include <stddef.h>
#include <stdint.h>

#define FL_AGENT_SECRET_LEN 16u
#define FL_AGENT_CRED_LEN 12u
#define FL_AGENT_MSG_MAX (256u * 1024u) /* OpenSSH's AGENT_MAX_LEN */
#define FL_AGENT_TEXT_MAX 64u           /* the socket file's text, NUL included */
#define FL_AGENT_HANDSHAKE_MS 5000

/* Agent protocol message numbers the relay knows. */
#define FL_AGENT_FAILURE 5
#define FL_AGENTC_REQUEST_IDENTITIES 11
#define FL_AGENTC_SIGN_REQUEST 13
#define FL_AGENTC_EXTENSION 27

/* The relay's listening socket and the file that points msys at it. */
struct fl_agent_relay {
    int lst;                               /* 127.0.0.1:port, non-blocking; -1 when closed */
    unsigned port;
    uint8_t secret[FL_AGENT_SECRET_LEN];
    char file[PATH_MAX];                   /* the msys socket file we wrote */
    char agent[108];                       /* $SSH_AUTH_SOCK (sun_path) */
    const char *net_tcp;                   /* "/proc/net/tcp" (tests point elsewhere) */
};

/* What one relay connection did (for the daemon's log line). */
struct fl_agent_result {
    const char *why;    /* "done", "auth-failed:<reason>", "agent-unreachable", "bad-message", ... */
    unsigned requests;  /* requests forwarded to the agent */
    unsigned refused;   /* requests answered with SSH_AGENT_FAILURE by the relay */
};

/*
 * The socket file's content for port and secret: "!<socket >PORT s W0-W1-W2-W3" with each
 * Wi the little-endian u32 of secret[4i..4i+4) in 8 lower-case hex digits, NUL included
 * (as Cygwin writes it). The length written (NUL included), or 0 when it does not fit cap.
 */
size_t fl_agent_socket_text(unsigned port, const uint8_t secret[FL_AGENT_SECRET_LEN], char *out, size_t cap);

/*
 * The owner uid of the IPv4 TCP socket local_addr:local_port -> 127.0.0.1:remote_port in a
 * /proc/net/tcp text (NUL-terminated; addresses as the kernel prints them, i.e. the
 * in_addr's s_addr; ports in host order). 0 and *uid set when found, -1 otherwise.
 */
int fl_agent_tcp_uid(const char *text, uint32_t local_addr, unsigned local_port, unsigned remote_port, uint32_t *uid);

/*
 * 1 when the agent request msg[0..len) (message number first, no length prefix) may reach
 * the agent: REQUEST_IDENTITIES, SIGN_REQUEST and the EXTENSION "session-bind@openssh.com".
 */
int fl_agent_request_allowed(const uint8_t *msg, size_t len);

/*
 * Start the relay: check that agent is an absolute path to a socket and that file's
 * directory is a directory of this user without group or other access, listen on
 * 127.0.0.1 (ephemeral port), draw the secret and write file (0600, O_EXCL temporary
 * file renamed into place, SYSTEM attribute). 0 on success; -1 with a short reason in
 * why (the relay is then closed and no file is left).
 */
int fl_agent_relay_open(struct fl_agent_relay *r, const char *file, const char *agent, char *why, size_t whycap);

/* Stop listening and remove the socket file while it still holds what we wrote (our secret). */
void fl_agent_relay_close(struct fl_agent_relay *r);

/*
 * Serve one accepted connection c from peer (in a forked child): check the peer's owner,
 * the handshake (within FL_AGENT_HANDSHAKE_MS), then relay requests until either side
 * closes. Closes c; fills *res.
 */
void fl_agent_serve(const struct fl_agent_relay *r, int c, const struct sockaddr_in *peer,
                    struct fl_agent_result *res);

#endif /* FL_AGENT_RELAY_H */
