/** @odoo-module **/

import { reactive } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { rpc } from "@web/core/network/rpc";
import { PilotSession } from "./pilot_session";
import { PilotPhone } from "./pilot_phone";

const HOSTNAME = /^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;
// Server answers that mean the caller's Odoo phone authorization is gone, not a transient failure.
const DENIALS = ["odoo.exceptions.AccessError", "odoo.exceptions.UserError", "odoo.http.SessionExpiredException"];

function isValidMapping(mapping) {
    // The phone talks only to its tenant's own HTTPS host.
    return typeof mapping?.tenant === "string" && HOSTNAME.test(mapping.tenant) &&
        mapping.origin === `https://${mapping.tenant}` &&
        typeof mapping.extension === "string" && /^[0-9]{2,6}$/.test(mapping.extension) &&
        Array.isArray(mapping.directory) && mapping.directory.length > 0 &&
        mapping.directory.every(item => typeof item?.extension === "string" &&
            /^[0-9]{2,6}$/.test(item.extension) && typeof item.name === "string") &&
        mapping.directory.some(item => item.extension === mapping.extension);
}

export const pilotService = {
    dependencies: ["notification"],
    async start(env, { notification }) {
        const [authorized, canManage] = await Promise.all([
            user.hasGroup("phone_system.group_pilot"),
            user.hasGroup("base.group_system"),
        ]);
        const state = reactive({
            authorized, canManage, dashboard: null, dashboardError: "",
            account: null, accountBusy: false, accountError: "",
            visible: false, minimized: false, extension: "", tenant: "",
            connection: "disconnected", call: "idle", direction: "",
            muted: false, connecting: false, error: "", playbackBlocked: false,
            directory: [], consentRequired: false, consentBusy: false, consentExtension: "", externalCalling: false,
            number: "", followUp: null,
        });
        if (canManage) {
            try {
                const dashboard = await rpc("/vodia/pilot/dashboard", {});
                if (dashboard?.can_manage !== true ||
                    typeof dashboard.managed_setup?.available !== "boolean" ||
                    typeof dashboard.managed_setup?.message !== "string" ||
                    typeof dashboard.existing_setup?.available !== "boolean" ||
                    typeof dashboard.existing_setup?.message !== "string") {
                    throw new Error("Invalid Phone System dashboard response.");
                }
                state.dashboard = dashboard;
            } catch {
                state.dashboardError = _t("Phone System setup status could not be loaded. Reload and try again.");
            }
        }
        let session = null;
        let phone = null;
        let audio = null;
        let releaseLock = null;
        let busy = false;
        let heartbeat = null;
        let recoveryTimer = null;
        let generation = 0;
        let pendingPeer = null;
        let callLog = null;

        async function accountRequest(route, params = {}) {
            if (!canManage || state.accountBusy) {
                return;
            }
            state.accountBusy = true;
            state.accountError = "";
            try {
                const result = await rpc(route, params);
                if (typeof result?.available !== "boolean" ||
                    !["unavailable", "unlinked", "verification_pending", "authorization_required", "mfa_required", "approval_required", "linked", "terms_required", "expired", "revoked"].includes(result.state) ||
                    typeof result.message !== "string" ||
                    (result.available === false) !== (result.state === "unavailable") ||
                    (["verification_pending", "authorization_required", "mfa_required", "approval_required", "linked", "terms_required"].includes(result.state) &&
                        (typeof result.email !== "string" || typeof result.expires_at !== "string"))) {
                    throw new Error("Invalid GCNA account status.");
                }
                if (result.invitation && (typeof result.invitation.company !== "string" ||
                    typeof result.invitation.can_activate !== "boolean")) {
                    throw new Error("Invalid GCNA invitation status.");
                }
                state.account = {
                    available: result.available, state: result.state, message: result.message,
                    email: result.email || "", expires_at: result.expires_at || "",
                    invitation: result.invitation ? {
                        company: result.invitation.company, can_activate: result.invitation.can_activate,
                        imprint: result.invitation.imprint, terms: result.invitation.terms,
                        privacy: result.invitation.privacy,
                    } : null,
                };
            } catch (error) {
                state.account = null;
                state.accountError = error.data?.name === "odoo.exceptions.UserError" &&
                    typeof error.data.message === "string"
                    ? error.data.message
                    : _t("GCNA account linking could not be completed. Refresh the status or contact support.");
            } finally {
                state.accountBusy = false;
            }
        }

        function trackCall(detail) {
            if (detail.state !== "idle" && callLog?.call_id !== detail.id) {
                callLog = {
                    call_id: detail.id, direction: detail.direction, started_at: Date.now(),
                    peer: detail.direction === "outgoing" ? pendingPeer : detail.remote, answeredAt: null,
                    company_id: user.activeCompany.id,
                };
            }
            if (callLog && detail.state === "active" && !callLog.answeredAt) {
                callLog.answeredAt = Date.now();
            }
            if (detail.state === "idle") {
                finishLog();
            }
        }

        function finishLog() {
            const log = callLog;
            callLog = null;
            pendingPeer = null;
            if (!log) {
                return;
            }
            const { company_id, answeredAt, ...entry } = log;
            entry.outcome = answeredAt ? "answered" : (log.direction === "incoming" ? "missed" : "failed");
            entry.duration = answeredAt ? Math.min(86400, Math.round((Date.now() - answeredAt) / 1000)) : 0;
            rpc("/vodia/pilot/call", { company_id, entry }).then(async () => {
                if (entry.outcome !== "answered") {
                    return;
                }
                // After an answered call with someone outside, offer to send the user's contact info.
                const offer = await rpc("/vodia/pilot/contact_card/follow_up", { company_id, call_id: entry.call_id });
                if (offer?.prompt === true) {
                    state.followUp = { email: offer.email || "", name: offer.name || "", number: offer.number || "" };
                }
            }).catch(() => {});
        }

        function report(message) {
            state.error = message;
            notification.add(message, { type: "danger", title: _t("Phone") });
        }

        function disconnect() {
            ++generation;
            clearInterval(heartbeat);
            heartbeat = null;
            clearTimeout(recoveryTimer);
            recoveryTimer = null;
            finishLog();
            const oldPhone = phone;
            const oldSession = session;
            phone = session = null;
            oldPhone?.dispose();
            oldSession?.disconnect();
            if (audio) {
                audio.pause();
                audio.srcObject = null;
                audio = null;
            }
            releaseLock?.();
            releaseLock = null;
            state.connection = "disconnected";
            state.call = "idle";
            state.direction = "";
            state.extension = "";
            state.tenant = "";
            state.muted = false;
            state.playbackBlocked = false;
            state.directory = [];
            state.externalCalling = false;
            state.number = "";
            state.followUp = null;
        }

        function requireCaller() {
            if (!authorized || !phone) {
                throw new Error(_t("Connect an authorized phone session first."));
            }
        }

        async function acquireLock(tenant) {
            if (!navigator.locks) {
                throw new Error(_t("This browser cannot enforce single-tab phone ownership."));
            }
            await new Promise((resolve, reject) => {
                navigator.locks.request(
                    `vodia-pilot:${tenant}`,
                    { ifAvailable: true },
                    async lock => {
                        if (!lock) {
                            reject(new Error(_t("The phone system is already connected in another Odoo tab.")));
                            return;
                        }
                        const held = new Promise(resolve => { releaseLock = resolve; });
                        resolve();
                        await held;
                    }
                ).catch(reject);
            });
        }

        const api = {
            state,
            loadAccount() {
                return accountRequest("/vodia/account/resume");
            },
            requestAccountVerification(email) {
                return accountRequest("/vodia/account/request", { email });
            },
            confirmAccountVerification(code) {
                return accountRequest("/vodia/account/confirm", { code });
            },
            loginAccount(password) {
                return accountRequest("/vodia/account/login", { password });
            },
            verifyAccountMfa(code) {
                return accountRequest("/vodia/account/mfa", { code });
            },
            approveAccount(accept) {
                return accountRequest("/vodia/account/approve", { accept });
            },
            inspectAccountInvitation(token) {
                return accountRequest("/vodia/account/invitation", { token });
            },
            activateAccountInvitation({ name, password, accept_terms, terms_version, privacy_version }) {
                return accountRequest("/vodia/account/activate", {
                    name, password, accept_terms, terms_version, privacy_version,
                });
            },
            toggle() {
                if (authorized) {
                    state.visible = !state.visible;
                }
            },
            minimize() {
                state.minimized = !state.minimized;
            },
            async connect() {
                if (busy || session) {
                    return;
                }
                if (!authorized) {
                    report(_t("An authorized, disconnected phone session is required."));
                    return;
                }
                busy = true;
                const attempt = ++generation;
                state.connecting = true;
                state.error = "";
                try {
                    const companyId = user.activeCompany.id;
                    const mapping = await rpc("/vodia/pilot/configuration", { company_id: companyId });
                    if (mapping?.credential_required === true) {
                        // The user has not shared their PBX login yet; ask for consent instead of failing.
                        state.consentRequired = true;
                        state.consentExtension = typeof mapping.extension === "string" ? mapping.extension : "";
                        state.visible = true;
                        state.minimized = false;
                        return;
                    }
                    if (!isValidMapping(mapping)) {
                        throw new Error("Invalid mapping.");
                    }
                    const extension = mapping.extension;
                    const { origin, tenant } = mapping;
                    await acquireLock(tenant);
                    if (attempt !== generation) {
                        throw new Error("Connection was cancelled.");
                    }
                    const credential = await rpc("/vodia/pilot/session", { company_id: companyId });
                    if (attempt !== generation) {
                        throw new Error("Connection was cancelled.");
                    }
                    if (credential.origin !== origin || credential.tenant !== tenant ||
                        credential.extension !== extension || !credential.login) {
                        throw new Error("Invalid scoped session.");
                    }
                    session = new PilotSession();
                    const ownedSession = session;
                    phone = new PilotPhone({ session, destinations: mapping.directory.map(item => item.extension) });
                    phone.setExternalCalling(mapping.external_calling === true);
                    audio = new Audio();
                    audio.autoplay = true;
                    session.addEventListener("state", event => {
                        state.connection = event.detail;
                    });
                    session.addEventListener("failure", () => {
                        if (session !== ownedSession) {
                            return;
                        }
                        if (busy) {
                            report(_t("Phone signaling failed while connecting. Check your mapping and network."));
                            return;
                        }
                        disconnect();
                        report(_t("Phone signaling disconnected. Reconnecting automatically with a fresh login."));
                        recoveryTimer = setTimeout(() => {
                            recoveryTimer = null;
                            void api.connect();
                        }, 1000);
                    });
                    phone.addEventListener("call", event => {
                        trackCall(event.detail);
                        state.call = event.detail.state;
                        state.direction = event.detail.direction;
                        if (state.call === "ringing" && state.direction === "incoming") {
                            state.visible = true;
                            state.minimized = false;
                        }
                        if (state.call === "idle") {
                            state.muted = false;
                            state.playbackBlocked = false;
                            audio?.pause();
                            if (audio) {
                                audio.srcObject = null;
                            }
                        }
                    });
                    phone.addEventListener("failure", () => {
                        report(_t("Call failed. Check microphone permission and the signaling connection."));
                    });
                    phone.addEventListener("remote-stream", event => {
                        const player = audio;
                        player.srcObject = event.detail;
                        player.play().catch(() => {
                            if (audio !== player || !player.srcObject) {
                                return;
                            }
                            state.playbackBlocked = true;
                            report(_t("Audio playback was blocked. Click Enable audio in the phone panel."));
                        });
                    });
                    await session.connect({ origin, tenant, extension, login: credential.login });
                    if (attempt !== generation) {
                        throw new Error("Connection was cancelled.");
                    }
                    state.extension = extension;
                    state.tenant = tenant;
                    state.directory = mapping.directory;
                    state.externalCalling = mapping.external_calling === true;
                    state.number = typeof mapping.number === "string" && /^\+[0-9][0-9 ]{6,24}$/.test(mapping.number) ? mapping.number : "";
                    heartbeat = setInterval(async () => {
                        const revoke = () => {
                            disconnect();
                            report(_t("Your Odoo phone authorization expired or changed. Sign in again or check your mapping."));
                        };
                        let current;
                        try {
                            current = await rpc("/vodia/pilot/configuration", { company_id: companyId });
                        } catch (error) {
                            // A network or server hiccup must not end a live call; only a real denial does.
                            if (attempt === generation && DENIALS.includes(error?.data?.name)) {
                                revoke();
                            }
                            return;
                        }
                        if (attempt !== generation) {
                            return;
                        }
                        if (user.activeCompany.id !== companyId || !isValidMapping(current) ||
                            current.extension !== extension || current.tenant !== tenant) {
                            revoke();
                            return;
                        }
                        const listed = items => items.map(item => `${item.extension}:${item.name}`).join(",");
                        if (listed(current.directory) !== listed(state.directory)) {
                            phone.setDestinations(current.directory.map(item => item.extension));
                            state.directory = current.directory;
                        }
                        if ((current.external_calling === true) !== state.externalCalling) {
                            state.externalCalling = current.external_calling === true;
                            phone.setExternalCalling(state.externalCalling);
                        }
                    }, 60000);
                } catch {
                    disconnect();
                    report(_t("Phone connection failed. Check your individual credential, mapping, browser cookie policy and single-tab ownership."));
                } finally {
                    busy = false;
                    state.connecting = false;
                }
            },
            async consent(extension, password) {
                if (state.consentBusy) {
                    return;
                }
                state.consentBusy = true;
                state.error = "";
                try {
                    await rpc("/vodia/pilot/consent", { company_id: user.activeCompany.id, extension, password, consent: true });
                    state.consentRequired = false;
                } catch (error) {
                    state.error = error.data?.name === "odoo.exceptions.UserError" && typeof error.data.message === "string"
                        ? error.data.message
                        : _t("Your PBX login could not be confirmed. Try again.");
                    return;
                } finally {
                    state.consentBusy = false;
                }
                await api.connect();
            },
            disconnect() {
                if (busy) {
                    report(_t("Wait for the connection attempt to finish."));
                    return;
                }
                disconnect();
            },
            async dial(destination) {
                try {
                    requireCaller();
                    pendingPeer = destination;
                    await phone.dial(destination);
                } catch {
                    report(_t("Dialing failed. Choose an authorized colleague and check microphone access."));
                }
            },
            // A number clicked anywhere in Odoo or typed on the dialpad: opens the phone, connects it when
            // needed and dials. Colleagues' extensions always work; other numbers need external calling.
            async dialNumber(raw) {
                const number = typeof raw === "string" ? raw.replace(/[\s().\/-]/g, "") : "";
                const extension = /^[0-9]{2,6}$/.test(number);
                if (!extension && !/^\+?[0-9]{7,15}$/.test(number)) {
                    report(_t("This number cannot be dialed."));
                    return;
                }
                state.visible = true;
                state.minimized = false;
                if (state.connection !== "ready") {
                    await api.connect();
                }
                if (state.connection !== "ready") {
                    return;
                }
                if (!extension && !state.externalCalling) {
                    report(_t("You may not call outside numbers yet. Ask an administrator to allow external calling."));
                    return;
                }
                await api.dial(number);
            },
            // The user's own contact card, mailed by GCNA as a courtesy; the address is suggested from the
            // last call with a known contact.
            dismissFollowUp() {
                state.followUp = null;
            },
            async contactCardRecipient() {
                try {
                    const { email } = await rpc("/vodia/pilot/contact_card/recipient", { company_id: user.activeCompany.id });
                    return typeof email === "string" ? email : "";
                } catch {
                    return "";
                }
            },
            async sendContactCard(email) {
                try {
                    await rpc("/vodia/pilot/contact_card", { company_id: user.activeCompany.id, email });
                    notification.add(_t("Your contact info is on its way."), { type: "success" });
                    return true;
                } catch (error) {
                    report(error?.data?.message || _t("Your contact info could not be sent. Retry later."));
                    return false;
                }
            },
            // Accepted GCNA invitations whose recipient is not yet a contact; the user creates it on purpose.
            async networkPending() {
                try {
                    const { connections } = await rpc("/vodia/pilot/network/pending", { company_id: user.activeCompany.id });
                    return Array.isArray(connections) ? connections : [];
                } catch {
                    return [];
                }
            },
            async createNetworkContact(invitationId) {
                try {
                    const contact = await rpc("/vodia/pilot/network/contact", { company_id: user.activeCompany.id, invitation_id: invitationId });
                    notification.add(_t("%s was added to your contacts.", contact.name), { type: "success" });
                    return true;
                } catch (error) {
                    report(error?.data?.message || _t("The contact could not be created."));
                    return false;
                }
            },
            async answer() {
                try {
                    requireCaller();
                    await phone.answer();
                } catch {
                    report(_t("The incoming call could not be answered."));
                }
            },
            hangup() {
                try {
                    requireCaller();
                    phone.hangup();
                } catch {
                    report(_t("Ending the call failed; disconnect the session if the call remains active."));
                }
            },
            mute() {
                try {
                    requireCaller();
                    phone.mute(!state.muted);
                    state.muted = !state.muted;
                } catch {
                    report(_t("Muting failed."));
                }
            },
            async play() {
                try {
                    requireCaller();
                    if (!audio?.srcObject) {
                        throw new Error("No remote audio stream.");
                    }
                    await audio.play();
                    state.playbackBlocked = false;
                } catch {
                    report(_t("Audio playback is unavailable."));
                }
            },
        };
        if (authorized) {
            window.addEventListener("pagehide", disconnect);
            void api.connect();
        }
        return api;
    },
};

registry.category("services").add("phone_system", pilotService);
