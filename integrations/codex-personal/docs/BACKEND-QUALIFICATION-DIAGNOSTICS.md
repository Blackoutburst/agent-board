# Bounded backend qualification observations

A failed qualification can carry a second in-memory diagnostic line with
`status=qualification_diagnostic` and `kind=linux_backend`. The original refusal
line, exit status, qualification criteria and persisted receipt schemas stay
unchanged. This line is an observation, never qualification or cleanup proof.

The copied closed payload names the nine-case enum, controller stage and last
observed job phase (or null). The phase freezes at the first control refusal;
later completion does not replace that observation. `controller` is null unless a control operation
was refused; it then preserves only the first fixed operation, failure bucket
and allowlisted code (or null). `native` is null unless a native observation
was already read; its only fields are reason, launch/SOURCE booleans, the exact
28 checks or an empty object, and the five cleanup booleans. False and null
values are retained. No native packet, output, identity, PID or path is exported.

During explicit Pool preparation, our authorization callback may append its
last `scope_check`: `held`, `fresh` or `scope`, an allowed boolean, a fixed failure
bucket or null, an allowlisted code or null, and elapsed milliseconds bounded
to 0–600000. A successful slow check remains visible if the enclosing control
window then expires. Unknown exceptions expose only a fixed bucket, never type
names or messages. Observation failures never replace the original refusal.
The callback still returns the same authorization result; diagnostics neither
renew a deadline nor add a read, retry, process, grant or RPC.

For example, an `eof_running` case with no SOURCE release and no init identity
is not proof that the intended post-ACK EOF injection happened. A controller
window/send refusal can precede that point. Its native cleanup remains false
and qualification remains refused; this diagnostic does not weaken that guard.
