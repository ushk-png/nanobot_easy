[Background job {{ job_id }} {{ outcome }}]

It was started earlier in this conversation and ended while no turn was waiting for it.

state: {{ state }}
exit_code: {{ exit_code }}
elapsed: {{ elapsed }}s
command: {{ command }}
log: {{ log_path }}

Last log lines:
{{ tail }}

{% if stalled %}
The log shows the job stopped at a permission prompt that nobody could answer, so it probably did not do the work even though it exited normally. Treat it as not done: do not ask the user to approve anything inside the job; start it again so that it can run without prompts.

{% endif %}
Check the result against what the user asked for (run the tests or look at the files the job produced), then tell the user the outcome in a few sentences.{% if not stalled %} Do not start the same command again.{% endif %}
