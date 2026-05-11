from typing import Dict


class PermissionGuard:
    HIGH_RISK = {
        "shell.run", "email.send", "payment.send",
        "github.push", "vercel.deploy",
    }

    def check(self, action: Dict) -> Dict:
        tool = action.get("tool")
        if tool in self.HIGH_RISK:
            return {
                "allowed": False,
                "requires_approval": True,
                "reason": f"High-risk tool blocked: {tool}",
            }
        return {"allowed": True, "requires_approval": False}
