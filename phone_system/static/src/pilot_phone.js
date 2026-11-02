/** @odoo-module **/

export class PilotPhone extends EventTarget {
    #session;
    #getMedia;
    #createPeer;
    #createId;
    #active = null;
    #signals = Promise.resolve();
    #signalListener;
    #stateListener;
    #disposed = false;
    #destinations;
    #external = false;

    constructor({
        session,
        getMedia = () => navigator.mediaDevices.getUserMedia({ audio: true, video: false }),
        createPeer = () => new RTCPeerConnection({ iceServers: [] }),
        createId = () => crypto.randomUUID().replaceAll("-", "").slice(0, 8) + "@app",
        destinations = ["201", "202"],
    }) {
        super();
        this.#session = session;
        this.setDestinations(destinations);
        this.#getMedia = getMedia;
        this.#createPeer = createPeer;
        this.#createId = createId;
        this.#signalListener = event => {
            this.#signals = this.#signals.then(() => this.#receive(event.detail))
                .catch(() => {
                    if (this.#active?.id === event.detail.callid) {
                        this.#fail("Call signaling failed.");
                    } else {
                        this.#notify("failure", new Error("Call signaling failed."));
                    }
                });
        };
        this.#stateListener = event => {
            if (event.detail === "error" || event.detail === "disconnected") {
                this.#fail("Call ended because signaling disconnected.");
            }
        };
        session.addEventListener("signal", this.#signalListener);
        session.addEventListener("state", this.#stateListener);
    }

    setDestinations(destinations) {
        if (!Array.isArray(destinations) || !destinations.length ||
            destinations.some(value => typeof value !== "string" || !/^[0-9]{2,6}$/.test(value))) {
            throw new Error("Provide an authorized internal extension directory.");
        }
        this.#destinations = new Set(destinations);
    }

    // External numbers (E.164 or national digits) go to the PBX only while the user may call outside.
    setExternalCalling(allowed) {
        this.#external = allowed === true;
    }

    get state() {
        return this.#active?.state ?? "idle";
    }

    #notify(type, detail) {
        this.dispatchEvent(new CustomEvent(type, { detail }));
    }

    #change(call, state) {
        call.state = state;
        this.#notify("call", { id: call.id, state, direction: call.direction, remote: call.remote ?? null });
    }

    #begin(id, direction) {
        if (this.#disposed || this.#active || this.#session.state !== "ready") {
            throw new Error("A ready, idle phone is required.");
        }
        const call = {
            id, direction, state: "preparing", peer: null, stream: null,
            candidates: [], localCandidates: [], sent: false, cseq: undefined,
            timer: setTimeout(() => this.#fail("Call setup timed out."), 60000),
        };
        this.#active = call;
        return call;
    }

    #current(call) {
        if (this.#active !== call) {
            throw new Error("Call was cancelled.");
        }
    }

    #send(call, message) {
        this.#current(call);
        this.#session.sendCall({ ...message, callid: call.id });
    }

    async #prepare(call) {
        const stream = await new Promise((resolve, reject) => {
            call.cancelMedia = () => reject(new Error("Call was cancelled."));
            this.#getMedia().then(stream => {
                if (this.#active !== call) {
                    stream.getTracks().forEach(track => track.stop());
                    reject(new Error("Call was cancelled."));
                } else {
                    resolve(stream);
                }
            }, reject);
        });
        call.cancelMedia = null;
        call.stream = stream;
        call.peer = this.#createPeer();
        for (const track of stream.getAudioTracks()) {
            call.peer.addTrack(track, stream);
        }
        call.peer.ontrack = event => {
            if (this.#active === call) {
                this.#notify("remote-stream", event.streams[0] ?? new MediaStream([event.track]));
            }
        };
        call.peer.onicecandidate = event => {
            if (!event.candidate || this.#active !== call) {
                return;
            }
            const candidate = event.candidate.toJSON();
            if (!call.sent) {
                call.localCandidates.push(candidate);
            } else {
                try {
                    this.#send(call, { action: "ice-candidate", candidate });
                } catch {
                    this.#fail("ICE delivery failed.");
                }
            }
        };
        call.peer.onconnectionstatechange = () => {
            if (this.#active === call && call.peer.connectionState === "failed") {
                this.#fail("Audio transport failed.");
            }
        };
    }

    #flushLocal(call) {
        call.sent = true;
        for (const candidate of call.localCandidates.splice(0)) {
            this.#send(call, { action: "ice-candidate", candidate });
        }
    }

    async #flushRemote(call) {
        for (const candidate of call.candidates.splice(0)) {
            this.#current(call);
            await call.peer.addIceCandidate({ candidate, sdpMLineIndex: 0 });
        }
    }

    async dial(destination) {
        const external = this.#external && typeof destination === "string" && /^\+?[0-9]{7,15}$/.test(destination);
        if (!this.#destinations.has(destination) && !external) {
            throw new Error("Only authorized internal extensions are allowed.");
        }
        const call = this.#begin(this.#createId(), "outgoing");
        this.#change(call, "preparing");
        try {
            await this.#prepare(call);
            const offer = await call.peer.createOffer();
            this.#current(call);
            await call.peer.setLocalDescription(offer);
            this.#send(call, { action: "sdp-packet", to: destination,
                sdp: { type: offer.type, sdp: offer.sdp } });
            this.#flushLocal(call);
            this.#change(call, "ringing");
        } catch {
            if (this.#active === call) {
                this.#fail("Call setup failed; check microphone permission and signaling.");
            }
            throw new Error("Call setup failed; check microphone permission and signaling.");
        }
    }

    async answer() {
        const call = this.#active;
        if (!call || call.direction !== "incoming" || call.state !== "ringing") {
            throw new Error("An incoming ringing call is required.");
        }
        this.#change(call, "answering");
        try {
            await this.#prepare(call);
            await call.peer.setRemoteDescription({ type: "offer", sdp: call.offer });
            this.#current(call);
            await this.#flushRemote(call);
            const answer = await call.peer.createAnswer();
            this.#current(call);
            await call.peer.setLocalDescription(answer);
            this.#send(call, { action: "sdp-200ok", cseq: call.cseq,
                sdp: { type: answer.type, sdp: answer.sdp } });
            this.#flushLocal(call);
            clearTimeout(call.timer);
            this.#change(call, "active");
        } catch {
            if (this.#active === call) {
                this.#fail("Answer failed; check microphone permission and signaling.");
            }
            throw new Error("Answer failed; check microphone permission and signaling.");
        }
    }

    async #receive(message) {
        if (this.#disposed) {
            return;
        }
        if (message.invitesdp) {
            if (this.#active?.id === message.callid) {
                return;
            }
            if (this.#active) {
                this.#session.sendCall({ action: "sip-bye", callid: message.callid });
                this.#notify("failure", new Error("A second incoming call was declined; the phone supports one call."));
                return;
            }
            if (typeof message.invitesdp !== "string" || typeof message.cseq !== "string") {
                throw new Error("Invalid incoming call.");
            }
            const call = this.#begin(message.callid, "incoming");
            call.cseq = message.cseq;
            call.remote = /^[0-9]{2,6}$/.test(message["from-user"]) ? message["from-user"] : null;
            call.offer = message.invitesdp;
            this.#send(call, { action: "sip-ringing", cseq: call.cseq });
            this.#change(call, "ringing");
            return;
        }
        const call = this.#active;
        if (!call || message.callid !== call.id) {
            return;
        }
        if (message.bye) {
            this.#send(call, { action: "bye-response", cseq: message.cseq });
            this.#release(call);
        } else if (message.cancel || message.code >= 300) {
            this.#fail("The remote endpoint cancelled or rejected the call.");
        } else if (message.sdp) {
            if (!call.peer || call.direction !== "outgoing" || typeof message.sdp !== "string" ||
                ![183, 200].includes(message.code)) {
                throw new Error("Invalid SDP answer.");
            }
            if (call.state !== "active") {
                await call.peer.setRemoteDescription({
                    type: message.code === 183 ? "pranswer" : "answer", sdp: message.sdp,
                });
                this.#current(call);
                await this.#flushRemote(call);
            }
            if (message.code === 200) {
                this.#send(call, { action: "sip-ack" });
                clearTimeout(call.timer);
                this.#change(call, "active");
            }
        } else if (message.candidate) {
            if (typeof message.candidate !== "string") {
                throw new Error("Invalid remote ICE candidate.");
            }
            call.candidates.push(message.candidate);
            if (call.peer?.remoteDescription) {
                await this.#flushRemote(call);
            }
        }
    }

    mute(enabled) {
        const call = this.#active;
        if (!call?.stream || typeof enabled !== "boolean") {
            throw new Error("An audio call and a boolean mute state are required.");
        }
        this.#send(call, { action: "mute", muted: enabled });
        for (const track of call.stream.getAudioTracks()) {
            track.enabled = !enabled;
        }
    }

    hangup() {
        const call = this.#active;
        if (!call) {
            throw new Error("No call is active.");
        }
        try {
            if (call.sent || call.direction === "incoming") {
                this.#send(call, { action: "sip-bye" });
            }
        } finally {
            this.#release(call);
        }
    }

    #release(call) {
        clearTimeout(call.timer);
        call.cancelMedia?.();
        call.cancelMedia = null;
        if (call.peer) {
            call.peer.ontrack = call.peer.onicecandidate = call.peer.onconnectionstatechange = null;
            call.peer.close();
        }
        call.stream?.getTracks().forEach(track => track.stop());
        if (this.#active === call) {
            this.#active = null;
            this.#notify("call", { id: call.id, state: "idle", direction: call.direction });
        }
    }

    #fail(message) {
        const call = this.#active;
        if (call) {
            if (this.#session.state === "ready" && (call.sent || call.direction === "incoming")) {
                try {
                    this.#send(call, { action: "sip-bye" });
                } catch {
                    this.#notify("failure", new Error("Hangup delivery failed."));
                }
            }
            this.#release(call);
            this.#notify("failure", new Error(message));
        }
    }

    dispose() {
        this.#disposed = true;
        if (this.#active) {
            try {
                this.hangup();
            } catch {
                this.#notify("failure", new Error("Hangup delivery failed during disposal."));
            }
        }
        this.#session.removeEventListener("signal", this.#signalListener);
        this.#session.removeEventListener("state", this.#stateListener);
    }
}
