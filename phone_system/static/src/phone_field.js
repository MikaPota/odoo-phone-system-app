/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { useState } from "@odoo/owl";
import { CharField } from "@web/views/fields/char/char_field";
import { PhoneField } from "@web/views/fields/phone/phone_field";

// Customised forms often show a phone number as plain text; these fields are phone numbers on every model.
const PHONE_FIELD_NAMES = ["phone", "mobile", "work_phone", "mobile_phone"];

// Every phone number in Odoo can be called with the user's phone; the call is logged like any other.
patch(PhoneField.prototype, {
    setup() {
        super.setup();
        this.pilot = useService("phone_system");
        this.pilotState = useState(this.pilot.state);
    },
    get vodiaCanDial() {
        return this.pilotState.authorized === true && Boolean(this.props.record.data[this.props.name]);
    },
    vodiaDial() {
        return this.pilot.dialNumber(this.props.record.data[this.props.name]);
    },
});

patch(CharField.prototype, {
    setup() {
        super.setup();
        // Text fields also render outside the backend, where the phone is not available.
        if (this.env.services?.phone_system) {
            this.pilot = useService("phone_system");
            this.pilotState = useState(this.pilot.state);
        }
    },
    get vodiaCanDial() {
        return PHONE_FIELD_NAMES.includes(this.props.name) && !this.props.isPassword &&
            this.pilotState?.authorized === true && Boolean(this.props.record.data[this.props.name]);
    },
    vodiaDial() {
        return this.pilot.dialNumber(this.props.record.data[this.props.name]);
    },
});
