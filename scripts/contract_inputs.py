"""Messages submitted to both plugin contract runners."""

PHASES = [
    [
        "cpinspect payload",
        "cporder",
        "cpstate",
        "cpfail",
        "cpnomatch",
        "cpstate",
        "cpadmin",
        "cpgroup",
        "cpgroup",
        "cpdirect",
        "cpdirect",
        "cpadmin",
    ],
    ["cpstate"],
]


def event_for(phase, number):
    direct = phase == 0 and number in (9, 10)
    actor = "actor"
    if phase == 0 and 8 <= number <= 11:
        actor = "actor2"
    if phase == 0 and number == 12:
        actor = "owner"
    return {
        "actor_id": actor,
        "group_id": "" if direct else "room",
        "adapter_id": "astrbot",
        "conversation_kind": "direct" if direct else "group",
        "message_id": f"m{number}",
    }
