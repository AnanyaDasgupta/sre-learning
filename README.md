# SRE learning

My notes and hands-on code from preparing for SRE interviews and learning new topics. Each folder is one topic, with a README that explains what I built, how to run it, and what I tested.

## Topics

| Topic | What's in it |
| --- | --- |
| [Kubernetes operators](kubernetes-operators/) | Two operators in Python with kopf: a **Scaler** that keeps a Deployment at a set replica count, and a **PodRestarter** that does a rolling restart every N minutes. CRDs, CRs, the operators, and the tests I ran on kind |
