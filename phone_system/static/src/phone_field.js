/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { useState } from "@odoo/owl";
import { PhoneField } from "@web/views/fields/phone/phone_field";

// Every phone number in Odoo can be called with the user's Vodia phone; the call is logged like any other.
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
