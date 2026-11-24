[Background job {{ job_id }} {{ outcome }}]

It was started earlier in this conversation and ended while no turn was waiting for it.

state: {{ state }}
exit_code: {{ exit_code }}
elapsed: {{ elapsed }}s
command: {{ command }}
log: {{ log_path }}

Last log lines:
{{ tail }}

Check the result against what the user asked for (run the tests or look at the files the job produced), then tell the user the outcome in a few sentences. Do not start the same command again.
