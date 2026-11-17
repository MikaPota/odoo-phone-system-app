from odoo import _, api, fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    bizzkickr_connected = fields.Boolean(compute="_compute_bizzkickr_connected", string="BIZZKICKR connection")

    @api.depends_context("uid")
    def _compute_bizzkickr_connected(self):
        # Whether the viewer is connected with this contact; other users' connections stay theirs.
        mine = set(self.env["bizzkickr.connection"].sudo().search(
            [("partner_id", "in", self.ids), ("user_id", "=", self.env.uid)]).partner_id.ids) if self.ids else set()
        for partner in self:
            partner.bizzkickr_connected = partner.id in mine

    def action_bizzkickr_send_card(self):
        """The user's BIZZKICKR card goes to this contact, with the link to connect."""
        self.ensure_one()
        self.env["bizzkickr.person"]._mine()._send_to(self.email)
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "type": "success", "message": _("Your BIZZKICKR card is on its way to %s.", self.email), "sticky": False}}
