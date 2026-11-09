/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { PhoneSystemCheckout } from "./checkout";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";
import { _t } from "@web/core/l10n/translation";

function connectionLabel(connection) {
    return {
        disconnected: _t("Disconnected"),
        authenticating: _t("Authenticating"),
        connecting: _t("Connecting"),
        registering: _t("Registering"),
        ready: _t("Ready"),
        error: _t("Connection error"),
    }[connection] || _t("Connection unavailable");
}

// Vodia keys are shown in groups of three, as the License Server and partner portal show them.
function licenseKeyLabel(key) {
    return typeof key === "string" && /^[A-Za-z0-9]{12}$/.test(key) ? key.match(/.{3}/g).join("-") : key;
}

export class PilotPanel extends Component {
    static template = "phone_system.Panel";
    static props = {};

    setup() {
        this.pilot = useService("phone_system");
        this.state = useState(this.pilot.state);
        this.form = useState({ destination: "", number: "", extension: "", password: "", agreed: false });
        this.card = useState({ open: false, email: "", busy: false, connections: [] });
    }

    async openContactCard() {
        this.card.open = !this.card.open;
        if (this.card.open) {
            const [email, connections] = await Promise.all([
                this.card.email ? this.card.email : this.pilot.contactCardRecipient(), this.pilot.networkPending()]);
            this.card.email = email;
            this.card.connections = connections;
        }
    }

    // The offer after a call: the address is suggested from the matched contact and can be changed.
    get followUpPrompt() {
        const offer = this.state.followUp;
        return _t("Stay in contact with %s? Send your contact info.", offer.name || offer.number);
    }

    async sendFollowUp() {
        const email = this.form.followUpEmail ?? this.state.followUp.email;
        this.card.busy = true;
        try {
            if (await this.pilot.sendContactCard(email)) {
                this.dismissFollowUp();
            }
        } finally {
            this.card.busy = false;
        }
    }

    dismissFollowUp() {
        this.form.followUpEmail = undefined;
        this.pilot.dismissFollowUp();
    }

    async createNetworkContact(connection) {
        if (await this.pilot.createNetworkContact(connection.invitation_id)) {
            this.card.connections = this.card.connections.filter((c) => c !== connection);
        }
    }

    async sendContactCard() {
        this.card.busy = true;
        try {
            if (await this.pilot.sendContactCard(this.card.email)) {
                this.card.open = false;
                this.card.email = "";
            }
        } finally {
            this.card.busy = false;
        }
    }

    get connectionLabel() {
        return connectionLabel(this.state.connection);
    }

    async submitConsent() {
        await this.pilot.consent(this.form.extension || this.state.consentExtension, this.form.password);
        this.form.password = "";
    }

    get callLabel() {
        return {
            idle: _t("No call"),
            preparing: _t("Preparing microphone"),
            ringing: this.state.direction === "incoming" ? _t("Incoming call") : _t("Calling"),
            answering: _t("Answering"),
            active: _t("Call active"),
        }[this.state.call] || _t("Call unavailable");
    }

}

export class PilotSystray extends Component {
    static template = "phone_system.Systray";
    static props = {};

    setup() {
        this.pilot = useService("phone_system");
        this.state = useState(this.pilot.state);
    }
}

export class PhoneSystemAction extends Component {
    static template = "phone_system.App";
    static props = ["*"];
    static components = { PhoneSystemCheckout };

    setup() {
        this.pilot = useService("phone_system");
        this.state = useState(this.pilot.state);
        this.action = useService("action");
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.ui = useState({ checkout: false, system: null, systems: [], connections: [], systemConnections: {} });
        onWillStart(() => this.loadSystems());
    }

    get purchasable() {
        return this.state.dashboard?.managed_setup.available === true;
    }

    async loadSystems() {
        if (!this.state.canManage) {
            return;
        }
        // Existing systems connected in Odoo; purchased ones are listed from their own status below.
        this.ui.connections = await this.orm.searchRead("vodia.pilot.connection",
            [["gcna_system_id", "=", false], ["company_id", "=", (user.activeCompany?.id ?? user.context.allowed_company_ids[0])]],
            ["name", "pbx_host", "user_count", "linked_count", "has_own_phone"]);
        if (!this.state.dashboard?.managed_setup.available) {
            return;
        }
        try {
            this.ui.systems = (await rpc("/vodia/systems", {})).systems;
        } catch {
            this.ui.systems = [];
        }
        // A purchased system has its Odoo connection once users are connected.
        const connected = await this.orm.searchRead("vodia.pilot.connection",
            [["gcna_system_id", "in", this.ui.systems.map((system) => system.id)]],
            ["gcna_system_id", "user_count", "linked_count", "has_own_phone"]);
        this.ui.systemConnections = Object.fromEntries(connected.map(({ gcna_system_id, user_count, linked_count, has_own_phone }) =>
            [gcna_system_id, { user_count, linked_count, has_own_phone }]));
    }

    get connectionLabel() {
        return connectionLabel(this.state.connection);
    }

    // Connected and purchased systems share one card; a purchased one shows its setup until it is done.
    get cards() {
        const connected = this.ui.connections.map((connection) => ({
            key: `connection-${connection.id}`, badge: connection.pbx_host, host: connection.pbx_host, subtitle: connection.name,
            user_count: connection.user_count, linked_count: connection.linked_count, has_own_phone: connection.has_own_phone,
            system: null, setupDone: true, license: null, openSettings: () => this.openConnection(connection.id),
        }));
        const purchased = this.ui.systems.map((system) => {
            const connection = this.ui.systemConnections[system.id];
            return {
                key: `system-${system.id}`, badge: system.code, host: system.hostname, subtitle: this.capacityLabel(system),
                user_count: connection?.user_count || 0, linked_count: connection?.linked_count || 0,
                has_own_phone: connection?.has_own_phone === true, system,
                license: system.license ? { key: licenseKeyLabel(system.license.key), valid: system.license.valid } : null,
                cancelled: system.cancelled ? this.cancelledLabel(system.cancelled) : null,
                deleted: system.cancelled?.deleted === true,
                setupDone: ["paid", "provisioned", "users"].every((stage) => system.stages[stage]?.state === "complete"),
                openSettings: () => this.openSettings(system),
            };
        });
        return [...connected, ...purchased];
    }

    cancelledLabel(cancelled) {
        if (cancelled.deleted) {
            return _t("Cancelled. The phone system and its data are deleted.");
        }
        const date = new Date(cancelled.deleteAt).toLocaleDateString(user.lang || undefined);
        return _t("Cancelled. The phone system and its data will be deleted on %s.", date);
    }

    capacityLabel(system) {
        return _t("Up to %s users", system.capacity);
    }

    stageLabel(stage) {
        return {
            complete: _t("Done"), in_progress: _t("In progress"), pending: _t("Pending"), action_required: _t("Action required"),
        }[stage?.state] || _t("Pending");
    }

    async openSettings(system) {
        const [connectionId] = await this.orm.search("vodia.pilot.connection", [["gcna_system_id", "=", system.id]], { limit: 1 });
        if (connectionId) {
            return this.openConnection(connectionId);
        }
        if (system.stages.users?.state === "complete") {
            this.notification.add(_t("%s has connected users but no Odoo connection. Contact your service provider.", system.code), { type: "danger" });
            return;
        }
        // The connection is created when users are connected, so its setup continues there.
        this.ui.system = system;
        this.ui.checkout = true;
    }

    // Call records are kept for the current and the previous calendar month (UTC), then deleted for good.
    get callRecordMonths() {
        const now = new Date();
        return [0, -1].map((offset) => {
            const month = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + offset, 1));
            return {
                month: month.toISOString().slice(0, 7),
                label: month.toLocaleDateString(user.lang || undefined, { month: "long", year: "numeric", timeZone: "UTC" }),
            };
        });
    }

    callRecordsUrl(system, month) {
        return `/vodia/systems/call-records?system_id=${encodeURIComponent(system.id)}&month=${month}`;
    }

    async openBilling(system) {
        try {
            const { url } = await rpc("/vodia/systems/billing", { system_id: system.id });
            window.location.assign(url);
        } catch (error) {
            this.notification.add(error.data?.message || _t("The subscription page could not be opened."), { type: "danger" });
        }
    }

    async openConnection(connectionId) {
        await this.action.doAction(connectionId
            ? { type: "ir.actions.act_window", res_model: "vodia.pilot.connection", res_id: connectionId, views: [[false, "form"]] }
            : "phone_system.action_connections");
    }

    openConnect() {
        return this.action.doAction("phone_system.action_connect_wizard", { onClose: () => this.loadSystems() });
    }

    openCheckout() {
        this.ui.system = null;
        this.ui.checkout = true;
    }

    async closeCheckout() {
        this.ui.checkout = false;
        this.ui.system = null;
        await this.loadSystems();
    }
}

registry.category("actions").add("phone_system.open", PhoneSystemAction);
registry.category("main_components").add("phone_system.Panel", { Component: PilotPanel });
registry.category("systray").add("phone_system.Systray", { Component: PilotSystray }, { sequence: 20 });
