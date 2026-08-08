"""Every agent inherits from Agent so every action gets logged the same way:
to the console (for the live demo) and to the agent_trace table (so the
full reasoning chain for any incident can be replayed later -- this is the
'Explainability' pillar of the project)."""
from rich.console import Console

from tools import crdb_client

console = Console()


class Agent:
    name = "agent"

    def log(self, incident_id, step, detail=""):
        console.print(f"[bold cyan]{self.name}[/bold cyan] · {step} — {detail}")
        try:
            crdb_client.run_query(
                """INSERT INTO agent_trace (incident_id, agent_name, step, detail)
                   VALUES (%s, %s, %s, %s)""",
                (incident_id, self.name, step, detail),
                fetch=False,
            )
        except Exception as e:
            console.print(f"[red]trace log failed: {e}[/red]")
