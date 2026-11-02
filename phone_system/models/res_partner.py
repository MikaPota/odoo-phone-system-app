from odoo import _, api, fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    vodia_call_count = fields.Integer(compute="_compute_vodia_call_count", string="Calls")
    vodia_network_connected = fields.Boolean(compute="_compute_vodia_network_connected", string="GCNA connected")

    def _compute_vodia_network_connected(self):
        # Shown to everyone who sees the contact; who sent the invitation stays private to its sender.
        connected = set(self.env["vodia.network.invitation"].sudo().search(
            [("partner_id", "in", self.ids), ("state", "=", "connected")]).partner_id.ids) if self.ids else set()
        for partner in self:
            partner.vodia_network_connected = partner.id in connected

    @api.depends_context("uid")
    def _compute_vodia_call_count(self):
        # Counted with the viewer's own call-history access: their calls, or all of them for administrators.
        Call = self.env["vodia.pilot.call"]
        counts = {}
        if Call.has_access("read") and self.ids:
            counts = {partner.id: count for partner, count in Call._read_group(
                [("peer_partner_id", "in", self.ids)], ["peer_partner_id"], ["__count"])}
        for partner in self:
            partner.vodia_call_count = counts.get(partner.id, 0)

    def action_vodia_calls(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "name": _("Calls"), "res_model": "vodia.pilot.call",
            "view_mode": "list", "domain": [("peer_partner_id", "=", self.id)],
            "context": {"create": False},
        }
