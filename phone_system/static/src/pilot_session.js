/** @odoo-module **/

/** An error that names the connection step that failed, so the panel can say what to fix. */
function failed(message, step) {
    return Object.assign(new Error(message), { step });
}

export class PilotSession extends EventTarget {
    #fetch;
    #WebSocket;
    #socket = null;
    #opening = false;
    #generation = 0;
    #rejectOpen = null;
    #deadline = null;
    #renewal = null;
    #state = "disconnected";
    #useragent = "Vodia-Odoo-Pilot/0.2";

    constructor({ fetch = globalThis.fetch, WebSocket = globalThis.WebSocket } = {}) {
        super();
        this.#fetch = (url, options) => fetch(url, options);
        this.#WebSocket = WebSocket;
    }

    get state() {
        return this.#state;
    }

    sendCall(message) {
        const actions = ["sdp-packet", "ice-candidate", "sip-ack", "sip-ringing",
            "sdp-200ok", "sip-bye", "bye-response", "mute"];
        if (this.#state !== "ready" || !actions.includes(message.action) ||
            typeof message.callid !== "string" || !message.callid) {
            throw new Error("Provide a call-scoped message on a ready phone session.");
        }
        this.#send(message);
    }

    #notify(type, detail) {
        this.dispatchEvent(new CustomEvent(type, { detail }));
    }

    #transition(state) {
        this.#state = state;
        this.#notify("state", state);
    }

    #clearTimers() {
        clearTimeout(this.#deadline);
        clearTimeout(this.#renewal);
        this.#deadline = null;
        this.#renewal = null;
    }

    #send(message) {
        if (!this.#socket || this.#socket.readyState !== this.#WebSocket.OPEN) {
            throw new Error("The signaling connection is not open.");
        }
        this.#socket.send(JSON.stringify(message));
    }

    async connect({ origin, tenant, extension, login, timeoutMs = 10000 }) {
        if (this.#opening || this.#socket) {
            throw new Error("Disconnect the existing phone session before connecting.");
        }
        const url = new URL(origin);
        // A tenant on GCNA's PBX is reached under GCNA's relay path; any other PBX at its own root.
        if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash ||
            (url.pathname !== "/" && !(url.host === "gcn-alliance.com" && url.pathname === `/phone/${tenant}/`))) {
            throw new Error("Provide an HTTPS PBX origin without credentials or a path.");
        }
        if (typeof tenant !== "string" || !/^[a-z0-9.-]+$/i.test(tenant) ||
            typeof extension !== "string" || !/^[0-9]+$/.test(extension) ||
            !login || login.method !== "login-password" || login.username !== extension ||
            typeof login.id !== "string" || !/^[0-9a-zA-Z_-]{8,128}$/.test(login.id) ||
            typeof login.hash !== "string" || !/^[0-9a-f]{64}$/.test(login.hash) ||
            !Number.isFinite(timeoutMs) || timeoutMs <= 0) {
            throw new Error("Provide a tenant, numeric extension, scoped login proof and positive timeout.");
        }
        const relayed = url.host === "gcn-alliance.com";
        let session = null;
        this.#opening = true;
        const generation = ++this.#generation;
        const abort = new AbortController();
        this.#transition("authenticating");
        try {
            await new Promise((resolve, reject) => {
                this.#rejectOpen = reject;
                this.#deadline = setTimeout(() => {
                    abort.abort();
                    reject(failed("Session exchange timed out.", "unreachable"));
                }, timeoutMs);
                this.#fetch(new URL("rest/system/login", url), {
                    method: "POST",
                    credentials: "include",
                    body: JSON.stringify(login),
                    signal: abort.signal,
                }).catch(() => {
                    throw failed("The phone system could not be reached.", "unreachable");
                }).then(async response => {
                    const answer = response.ok ? await response.json() : null;
                    if (answer?.success !== true) {
                        throw failed("Individual login was rejected.", "refused");
                    }
                    // GCNA's relay returns the session itself: Safari keeps no third-party cookie.
                    if (relayed && typeof answer.session === "string" && /^[A-Za-z0-9._~+/=-]{8,256}$/.test(answer.session)) {
                        session = answer.session;
                    }
                    const identityUrl = new URL("rest/system/session", url);
                    if (session) identityUrl.searchParams.set("session", session);
                    const identityResponse = await this.#fetch(identityUrl, {
                        credentials: "include", cache: "no-store", signal: abort.signal,
                    }).catch(() => {
                        throw failed("The phone system could not be reached.", "unreachable");
                    });
                    const identity = await identityResponse.json();
                    // An accepted login followed by an anonymous session means the browser dropped the cookie.
                    if (!identityResponse.ok || !["user", "admin"].includes(identity.type)) {
                        throw failed("Individual login or identity verification failed: the browser kept no session.", "cookies");
                    }
                    if (identity.type !== "user" || identity.cuser !== extension || identity.cdomain !== tenant) {
                        throw failed("Individual login or identity verification failed: another identity.", "identity");
                    }
                    resolve();
                }).catch(error => reject(error.step ? error
                    : failed("Individual login or identity verification failed.", "refused")));
            });
            login = null;
            if (generation !== this.#generation) {
                throw new Error("Connection was cancelled.");
            }
            clearTimeout(this.#deadline);
            this.#transition("connecting");
            const socketUrl = new URL("websocket", url);
            socketUrl.protocol = "wss:";
            socketUrl.searchParams.set("domain", tenant);
            socketUrl.searchParams.set("user", extension);
            if (session) socketUrl.searchParams.set("session", session);
            session = null;
            await new Promise((resolve, reject) => {
                this.#rejectOpen = reject;
                const socket = new this.#WebSocket(socketUrl.href);
                this.#socket = socket;
                this.#deadline = setTimeout(() => {
                    reject(failed("Registration timed out.", "signaling"));
                }, timeoutMs);
                socket.onopen = () => {
                    try {
                        this.#transition("registering");
                        this.#send({ action: "own-calls", subscribe: true });
                        this.#send({ action: "sip-register", useragent: this.#useragent });
                    } catch {
                        reject(failed("Bootstrap failed.", "signaling"));
                    }
                };
                socket.onmessage = event => {
                    try {
                        const message = JSON.parse(event.data);
                        if (!message || typeof message !== "object" || Array.isArray(message)) {
                            throw new Error("Invalid message envelope.");
                        }
                        if (message.action === "sip-register") {
                            if (typeof message.add !== "string" || !message.add ||
                                !Number.isFinite(message.expires) || message.expires <= 0) {
                                throw new Error("Invalid registration acknowledgement.");
                            }
                            clearTimeout(this.#deadline);
                            clearTimeout(this.#renewal);
                            this.#renewal = setTimeout(() => {
                                try {
                                    this.#deadline = setTimeout(() => {
                                        this.#fail(new Error("Registration renewal timed out."));
                                    }, timeoutMs);
                                    this.#send({ action: "sip-register", useragent: this.#useragent });
                                } catch {
                                    this.#fail(new Error("Registration renewal failed."));
                                }
                            }, Math.max(1, Math.min(message.expires / 2, message.expires - 15)) * 1000);
                            this.#transition("ready");
                            resolve();
                        } else if (message.action === "call-state") {
                            if (!Array.isArray(message.calls)) {
                                throw new Error("Invalid own-call state.");
                            }
                            this.#notify("calls", message.calls);
                        }
                        if (message.callid) {
                            this.#notify("signal", message);
                        }
                    } catch {
                        this.#fail(new Error("Invalid signaling message."));
                        reject(failed("Invalid signaling message.", "signaling"));
                    }
                };
                socket.onerror = () => {
                    const error = failed("WebSocket failed; check session authorization and browser cookie policy.", "signaling");
                    this.#fail(error);
                    reject(error);
                };
                socket.onclose = () => {
                    const error = failed("Signaling disconnected; a fresh session is required.", "signaling");
                    this.#fail(error);
                    reject(error);
                };
            });
            this.#rejectOpen = null;
        } catch (error) {
            abort.abort();
            if (generation === this.#generation) {
                this.#clearTimers();
                this.#closeSocket();
                this.#transition("error");
            }
            throw error;
        } finally {
            if (generation === this.#generation) {
                this.#opening = false;
                this.#rejectOpen = null;
            }
        }
    }

    #closeSocket() {
        if (this.#socket) {
            const socket = this.#socket;
            this.#socket = null;
            if (socket.readyState === this.#WebSocket.OPEN) {
                try {
                    socket.send(JSON.stringify({ action: "remove-registration", useragent: this.#useragent }));
                } catch {
                    this.#notify("failure", new Error("Registration removal could not be sent."));
                }
            }
            socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
            socket.close();
        }
    }

    #fail(error) {
        this.#clearTimers();
        this.#closeSocket();
        this.#transition("error");
        this.#notify("failure", error);
    }

    disconnect() {
        ++this.#generation;
        this.#opening = false;
        this.#clearTimers();
        this.#rejectOpen?.(new Error("Connection was cancelled."));
        this.#rejectOpen = null;
        this.#closeSocket();
        this.#transition("disconnected");
    }
}
