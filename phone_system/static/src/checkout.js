/** @odoo-module **/

import { Component, onMounted, onPatched, onWillStart, onWillUnmount, useRef, useState } from "@odoo/owl";
import { loadJS } from "@web/core/assets";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { user } from "@web/core/user";
import { useService } from "@web/core/utils/hooks";

const STRIPE_JS = "https://js.stripe.com/v3/";

// Purchase of a new phone system: plan, details, email code, embedded payment, setup, users.
export class PhoneSystemCheckout extends Component {
    static template = "phone_system.Checkout";
    static props = { onClose: Function, resume: { type: Object, optional: true }, system: { type: Object, optional: true } };

    setup() {
        this.orm = useService("orm");
        this.paymentRef = useRef("payment");
        this.state = useState({
            step: "plan", busy: false, error: "", catalog: null, term: "monthly", planId: "",
            form: { company_name: "", domain: "", admin_name: "", email: "", accept_terms: false },
            code: "", system: null, users: [], selected: [], expiresAt: null, tick: 0, provisioned: 0,
        });
        this.embedded = null;
        this.poll = null;
        this.ticker = setInterval(() => this.state.tick++, 1000);
        onWillStart(async () => {
            await this.run(async () => {
                // A phone system that exists without connected users continues its setup; it needs no plan catalog.
                if (this.props.system) {
                    this.state.system = this.props.system;
                    if (this.props.system.stages.provisioned?.state === "complete") {
                        await this.loadUsers();
                        this.state.step = "users";
                    } else {
                        this.state.step = "setup";
                    }
                    return;
                }
                const catalog = await rpc("/vodia/checkout/catalog", {});
                this.state.catalog = catalog;
                Object.assign(this.state.form, catalog.buyer);
                this.state.planId = catalog.plans[0]?.planId || "";
                const resumed = this.props.resume || await rpc("/vodia/checkout/resume", {});
                if (resumed.state === "verification_pending") {
                    this.state.step = "verify";
                } else if (resumed.state === "payment_required") {
                    this.payment = resumed;
                    this.state.step = "payment";
                } else if (resumed.state === "paid") {
                    this.state.step = "setup";
                }
            });
        });
        onMounted(() => this.afterStep());
        onPatched(() => this.afterStep());
        onWillUnmount(() => {
            clearInterval(this.poll);
            clearInterval(this.ticker);
            this.embedded?.destroy();
        });
    }

    get plans() {
        return (this.state.catalog?.plans || []).filter((plan) => plan.prices[this.state.term]);
    }

    get hasAnnual() {
        return (this.state.catalog?.plans || []).some((plan) => plan.prices.annual);
    }

    price(plan) {
        const { amount, currency } = plan.prices[this.state.term];
        return new Intl.NumberFormat(user.lang || undefined, { style: "currency", currency: currency.toUpperCase() })
            .format(amount / 100);
    }

    planTerms(option) {
        return this.state.term === "monthly"
            ? _t("per month · up to %s users", option.capacity)
            : _t("per year · up to %s users", option.capacity);
    }

    get consentLabel() {
        return _t("I accept the terms and have read the privacy notice of %s.", this.state.catalog.legal.imprint.entity);
    }

    get codeHint() {
        return _t("Enter the six-digit code we sent to %s.", this.state.form.email);
    }

    get title() {
        return this.props.system
            ? _t("Connect users to %s", this.props.system.code)
            : _t("Create a new phone system and connect with Odoo");
    }

    get usersHint() {
        return _t("Choose who gets a phone. Your plan includes up to %s users (%s selected).", this.state.system.capacity, this.state.selected.length);
    }

    get plan() {
        return this.plans.find((plan) => plan.planId === this.state.planId);
    }

    async run(fn) {
        this.state.busy = true;
        this.state.error = "";
        try {
            await fn();
        } catch (error) {
            this.state.error = error.data?.name === "odoo.exceptions.UserError" && typeof error.data.message === "string"
                ? error.data.message : _t("Something went wrong. Try again or contact your service provider.");
        } finally {
            this.state.busy = false;
        }
    }

    setTerm(term) {
        this.state.term = term;
        if (!this.plan) {
            this.state.planId = this.plans[0]?.planId || "";
        }
    }

    selectPlan(planId) {
        this.state.planId = planId;
    }

    goTo(step) {
        this.state.step = step;
    }

    async choosePlan() {
        if (!this.plan) {
            return;
        }
        if (!this.state.catalog.linked) {
            this.state.step = "details";
            return;
        }
        // A linked account already has its details and verified email: straight to payment.
        await this.run(async () => {
            this.payment = await rpc("/vodia/checkout/order", {
                plan_id: this.state.planId, term: this.state.term, catalog_version: this.state.catalog.catalog_version,
            });
            this.orderId = this.payment.order_id;
            this.state.step = "payment";
        });
    }

    async startCheckout() {
        const { catalog, form } = this.state;
        await this.run(async () => {
            const started = await rpc("/vodia/checkout/start", {
                ...form, plan_id: this.state.planId, term: this.state.term, catalog_version: catalog.catalog_version,
                terms_version: catalog.legal?.terms.version, privacy_version: catalog.legal?.privacy.version,
            });
            this.state.expiresAt = Date.parse(started.expires_at) || null;
            this.state.step = "verify";
        });
    }

    get codeExpired() {
        // state.tick keeps this reactive so the Resend button appears the moment the code lapses.
        return this.state.tick >= 0 && this.state.expiresAt !== null && Date.now() >= this.state.expiresAt;
    }

    get canResend() {
        // After expiry, or when a resumed checkout's window is unknown, offer a fresh code in place.
        return this.state.tick >= 0 && (this.state.expiresAt === null || Date.now() >= this.state.expiresAt);
    }

    async resend() {
        await this.run(async () => {
            const resent = await rpc("/vodia/checkout/resend", {});
            this.state.expiresAt = Date.parse(resent.expires_at) || null;
            this.state.code = "";
        });
    }

    async confirmCode() {
        const code = this.state.code;
        this.state.code = "";
        await this.run(async () => {
            this.payment = await rpc("/vodia/checkout/confirm", { code });
            this.state.step = this.payment.state === "paid" ? "setup" : "payment";
        });
    }

    async afterStep() {
        if (this.state.step === "payment" && this.payment && !this.embedded && !this.mounting && this.paymentRef.el) {
            this.mounting = true;
            await this.run(async () => {
                await loadJS(STRIPE_JS);
                const stripe = window.Stripe(this.payment.publishable_key);
                this.embedded = await stripe.initEmbeddedCheckout({
                    fetchClientSecret: async () => this.payment.client_secret,
                    onComplete: () => {
                        this.embedded?.destroy();
                        this.embedded = null;
                        this.state.step = "setup";
                    },
                });
                this.embedded.mount(this.paymentRef.el);
            });
            this.mounting = false;
        }
        if (this.state.step === "setup" && !this.poll) {
            const check = async () => {
                try {
                    const { systems } = await rpc("/vodia/systems", {});
                    const system = this.props.system ? systems.find((item) => item.id === this.props.system.id)
                        : this.orderId ? systems.find((item) => item.order_id === this.orderId) : systems.at(-1);
                    if (!system) {
                        return;
                    }
                    this.state.system = system;
                    if (system.stages.provisioned?.state === "complete") {
                        clearInterval(this.poll);
                        this.poll = null;
                        await this.loadUsers();
                        this.state.step = "users";
                    }
                } catch {
                    // The next check retries; payment confirmation can take a moment.
                }
            };
            this.poll = setInterval(check, 3000);
            await check();
        }
    }

    async loadUsers() {
        this.state.users = await this.orm.searchRead(
            "res.users", [["share", "=", false], ["company_ids", "in", [(user.activeCompany?.id ?? user.context.allowed_company_ids[0])]]], ["name"], { order: "name" },
        );
    }

    toggleUser(id) {
        const selected = new Set(this.state.selected);
        if (selected.has(id)) {
            selected.delete(id);
        } else if (selected.size < this.state.system.capacity) {
            selected.add(id);
        }
        this.state.selected = [...selected];
    }

    async syncUsers() {
        await this.run(async () => {
            const result = await rpc("/vodia/systems/users", { system_id: this.state.system.id, user_ids: this.state.selected });
            this.state.provisioned = Object.keys(result?.extensions || {}).length;
            this.state.step = "done";
        });
    }

    get doneSummary() {
        const name = this.state.system?.name || _t("your phone system");
        return this.state.provisioned === 1
            ? _t("1 extension is live on %s.", name)
            : _t("%(count)s extensions are live on %(name)s.", { count: this.state.provisioned, name });
    }

    reload() {
        this.props.onClose();
        window.location.reload();
    }

    get stepIndex() {
        return ["plan", "details", "verify", "payment", "setup", "users", "done"].indexOf(this.state.step);
    }
}
