# Sentinel — Phase 1 Concepts, Tricks & Tips
# Terraform + Kubernetes Provider

Everything from building the Terraform modules for namespace management,
explained from the ground up with the non-obvious lessons highlighted.

---

## 1. What Terraform Actually Is

Terraform is a tool for declaring what infrastructure should exist and letting it
figure out how to get there. You write HCL (HashiCorp Configuration Language) files
describing resources. Terraform compares that declaration against what it knows
currently exists (the state file), and produces a plan of creates/updates/deletes
to reconcile the two.

```
Your HCL (desired)  ─┐
                      ├──► terraform plan ──► diff ──► terraform apply ──► real infra
State file (known)  ─┘
```

The critical mental model: **Terraform does not talk to infrastructure directly
when planning. It talks to the state file.** The state file is its memory of what
it previously created. The plan is a diff between the state file and your HCL.

---

## 2. The State File

`terraform.tfstate` is a JSON file that records every resource Terraform manages —
its type, configuration, and the real-world ID Kubernetes or the cloud gave it.

```json
{
  "resources": [{
    "type": "kubernetes_namespace",
    "name": "app",
    "instances": [{"attributes": {"id": "sentinel-app", ...}}]
  }]
}
```

**Why this matters in practice:**

- If you delete the state file, Terraform forgets everything it created. The next
  `plan` will show everything as "to create" — even if those resources already exist
  in the cluster. It will then fail on apply (can't create what already exists).
- If you manually delete a resource (e.g., `kubectl delete namespace sentinel-app`)
  but don't update state, the next `plan` detects the drift (resource in state but
  not in cluster) and plans to recreate it.
- **Never manually edit the state file.** Use `terraform state` subcommands instead.

**Trick — view current state:**
```bash
terraform state list                              # all managed resources
terraform state show module.kubernetes.kubernetes_namespace.app  # one resource
```

---

## 3. The plan → apply Workflow

`terraform plan` is a dry run. It shows exactly what will change before anything
is touched. Always read it before applying.

The symbols in plan output:
```
+ create     # new resource Terraform will create
~ update     # existing resource Terraform will modify in-place
- destroy    # resource Terraform will delete
-/+ replace  # must destroy and recreate (e.g., immutable field changed)
```

**The -/+ replace is the dangerous one.** A StatefulSet name change, a PVC rename,
or an immutable pod spec field will show `-/+` — meaning your database gets deleted
and recreated. Always read the full plan. Never blindly `-auto-approve` in production.

**Trick — save a plan and apply it exactly:**
```bash
terraform plan -out=tfplan     # save the plan
terraform apply tfplan         # apply exactly that plan, no re-evaluation
```
This guarantees what you reviewed is what gets applied. Without `-out`, a few
seconds between plan and apply could mean state changes (someone else applied
something) that alter what gets executed.

---

## 4. HCL Syntax Essentials

### Resources
```hcl
resource "kubernetes_namespace" "app" {
  #      ^^^^^^^^^^^^^^^^^^^^ type    ^ local name (used to reference this resource)
  metadata {
    name   = "sentinel-app"
    labels = var.labels         # reference a variable
  }
}
```

The address of this resource is `kubernetes_namespace.app`. Inside a module,
it's `module.kubernetes.kubernetes_namespace.app`.

### Variables
```hcl
variable "labels" {
  type      = map(string)   # type constraint — Terraform validates input
  default   = {}            # used when caller doesn't pass a value
  sensitive = true          # masks value in plan/apply output and logs
}
```

You reference variables with `var.labels`. Variables without defaults are
**required** — apply will error if they're not passed.

### Outputs
```hcl
output "namespace_app" {
  value = kubernetes_namespace.app.metadata[0].name
  #       ^^^^^^^^^^^^^^^^^^^^^^^^^^                resource type + local name
  #                                  ^^^^^^^^^      attribute path
}
```

Outputs expose values from a module to its caller. The root module can also
output values that `terraform output` prints after apply.

### Locals
```hcl
locals {
  common_labels = merge(var.labels, { "env" = "local" })
}
# reference with: local.common_labels
```

Locals are computed values used within a module. They reduce repetition — define
once, use many times. They're not exposed to callers (that's what outputs are for).

---

## 5. Module Structure

A module is just a directory with `.tf` files. The convention is:

```
modules/my-module/
  variables.tf   # what callers must/can pass in
  main.tf        # the actual resources
  outputs.tf     # what callers can read back
```

**Why separate files?** Terraform merges all `.tf` files in a directory — there's
no technical requirement to split them. The split is for humans: `variables.tf` is
a module's "public API", `outputs.tf` is what it "returns", `main.tf` is the
implementation.

**Calling a module:**
```hcl
module "kubernetes" {
  source = "../../modules/kubernetes"   # relative path to the module directory
  labels = { "team" = "platform" }      # override the default
}
```

After adding a new `module` block, you must run `terraform init` before `plan`.
Terraform needs to register the new module source (even for local paths). If you
forget, you get: `Error: Module not installed`.

**Referencing a module's output:**
```hcl
module "databases" {
  namespace = module.kubernetes.namespace_data   # output from kubernetes module
}
```

---

## 6. `depends_on` — Explicit vs Implicit Dependencies

Terraform automatically infers dependencies from resource references. If resource B
uses `resource_a.attr` in its config, Terraform knows A must exist before B. This
is an **implicit dependency**.

```hcl
# Implicit: Terraform sees the reference and knows ConfigMap must exist first
resource "helm_release" "postgresql" {
  set {
    name  = "initdb.scriptsConfigMap"
    value = kubernetes_config_map.pg_schema.metadata[0].name  # reference = implicit dep
  }
}
```

**Explicit `depends_on`** is for cases where there's no reference — you know a
dependency exists but Terraform can't see it:

```hcl
module "databases" {
  source    = "../../modules/databases"
  namespace = module.kubernetes.namespace_data

  depends_on = [module.kubernetes]   # explicit: databases need namespaces to exist first
}
```

Without this, Terraform might try to deploy PostgreSQL before `sentinel-data` namespace
exists, because it only sees the namespace name string being passed — not that the
namespace resource itself must be Ready first.

**Rule of thumb:** use `depends_on` at the module level when you're crossing module
boundaries. Use implicit references within a module.

---

## 7. `terraform import` — Adopting Existing Resources

When infrastructure was created outside of Terraform (manually, by a script, by
another tool), you can bring it under Terraform management with `import`.

```bash
terraform import <resource_address> <real_world_id>

# Example — adopt an existing namespace:
terraform import module.kubernetes.kubernetes_namespace.app sentinel-app
#               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^               resource address in HCL
#                                                           ^^^^^^^^^^^^  real name in Kubernetes
```

What import does:
1. Reads the real resource from Kubernetes
2. Writes it into the state file
3. Does NOT modify the real resource
4. Does NOT write any HCL — you must write the matching HCL yourself

After import, run `terraform plan`. It will show the diff between what's in state
(from the real resource) and what's in your HCL. Common outcomes:
- `0 changes` — your HCL matches the existing resource perfectly
- `~ update` — your HCL differs slightly (e.g., adding labels) — safe
- `-/+ replace` — your HCL requires destroying the resource — dangerous, fix the HCL

**What happened in Sentinel:** the 4 namespaces were created manually with `kubectl`
before Terraform existed. After writing the module, we ran import for each one.
The first plan showed `4 to add` (would have failed — can't create existing namespaces).
After import, the plan showed `4 to change` (just adding labels). Safe to apply.

**Trick:** if you have many resources to import, use `terraform import` in a loop:
```bash
for ns in sentinel-app sentinel-data sentinel-monitoring sentinel-pipeline; do
  terraform import "module.kubernetes.kubernetes_namespace.${ns#sentinel-}" "$ns"
done
```

---

## 8. Version Constraints

```hcl
required_providers {
  kubernetes = {
    source  = "hashicorp/kubernetes"
    version = "~> 2.35"   # pessimistic constraint operator
  }
}
```

`~> 2.35` means: `>= 2.35.0` AND `< 3.0.0`. It allows patch and minor updates
(2.36, 2.37…) but not major version bumps that might break APIs.

Other constraint operators:
```
= 2.35.0    # exact pin
>= 2.35     # any version at or above
~> 2.35     # 2.35.x or 2.x.y (allows minor, blocks major)
~> 2.35.0   # 2.35.x only (allows patch, blocks minor)
```

**Trick — the lock file:** `.terraform.lock.hcl` pins the exact installed versions.
Commit this file. When a teammate runs `terraform init`, they get the same provider
versions as you. Without it, they might get 2.38.0 while you're on 2.35.1 — subtle
behaviour differences that are painful to debug.

---

## 9. Path Variables in Modules

Terraform provides three path variables for referencing files:

| Variable | Value | Use case |
|---|---|---|
| `path.module` | absolute path to the current module directory | reference files co-located with the module |
| `path.root` | absolute path to the root module directory | reference files relative to where `terraform apply` is run |
| `path.cwd` | current working directory | rarely used |

**Sentinel's pattern:**
```hcl
# In terraform/modules/databases/main.tf
file("${path.module}/../../../db/postgres/migrations/001_initial_schema.sql")
# path.module = /home/vijay/projects/sentinel/terraform/modules/databases
# ../../../  = up to sentinel/ project root
# Result     = /home/vijay/projects/sentinel/db/postgres/migrations/001_initial_schema.sql
```

**Tip:** use `path.module` in modules (portable — works regardless of where terraform
apply is run from). Use `path.root` in root modules only.

---

## Key Lessons Summary

| Concept | The lesson |
|---|---|
| State file | Terraform's memory — it records what it created, not what exists. Drift between state and reality shows up on the next plan. |
| plan before apply | The plan is a contract. `-/+ replace` means destroy + recreate — always notice it before applying. |
| `terraform import` | Adopts existing resources without touching them. Write matching HCL first, then import, then plan to see the diff. |
| Module structure | variables.tf = API, main.tf = implementation, outputs.tf = return values. Three files, one concern each. |
| Implicit vs explicit `depends_on` | References create implicit deps (preferred). Use explicit `depends_on` only when crossing module boundaries with no resource reference. |
| `~> 2.35` | Allows minor/patch upgrades, blocks major. Commit `.terraform.lock.hcl` so the team gets the same provider version. |
| `path.module` | Use this in modules to reference files relative to the module directory — not `path.root`, not relative strings. |
| `terraform init` | Required whenever you add a new `module` or `provider` block. Terraform needs to register the source. |
