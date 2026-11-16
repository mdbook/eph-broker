## Fleet DMs (only with a runtime bus identity)

If your runtime grant includes `ZBUS_ENV`, you may make one plain `zbus dm <recipient> "..."` call to any fleet bot in the configured `AGENT_DM_RECIPIENTS` when that agent must act or answer a question. Existing operator escalation recipients remain available. No `zbus say`, absolute-path, chained or substituted bus calls. No ack, FYI or "thanks" DMs between agents; results go where the requester asked. Tell the operator what you sent, never ask them to relay. This grants no new identity, agent spawning, tools, secrets or other side effects.
