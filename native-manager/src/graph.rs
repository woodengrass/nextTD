//! Dependency ordering and reverse-dependency closure over the catalog.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Result, bail};

use crate::catalog::{Manifest, ROLE_HOST, ROLE_VIEW};

/// Dependencies before dependants; rejects missing nodes and cycles.
pub fn dependency_order(
    manifests: &BTreeMap<String, Manifest>,
    plugin_id: &str,
    errors: &BTreeMap<String, String>,
) -> Result<Vec<String>> {
    let mut completed: BTreeSet<String> = BTreeSet::new();
    let mut visiting: Vec<String> = Vec::new();
    let mut result: Vec<String> = Vec::new();

    fn visit(
        manifests: &BTreeMap<String, Manifest>,
        errors: &BTreeMap<String, String>,
        completed: &mut BTreeSet<String>,
        visiting: &mut Vec<String>,
        result: &mut Vec<String>,
        current: &str,
    ) -> Result<()> {
        if completed.contains(current) {
            return Ok(());
        }
        if visiting.iter().any(|visiting| visiting == current) {
            visiting.push(current.to_string());
            bail!("Plugin dependency cycle: {}", visiting.join(" -> "));
        }
        let Some(manifest) = manifests.get(current) else {
            if let Some(diagnostic) = errors.get(current) {
                bail!("{diagnostic}");
            }
            bail!("Required plugin not found: {current}");
        };
        visiting.push(current.to_string());
        for dependency in &manifest.requires_plugins {
            visit(manifests, errors, completed, visiting, result, dependency)?;
        }
        visiting.pop();
        completed.insert(current.to_string());
        result.push(current.to_string());
        Ok(())
    }

    visit(
        manifests,
        errors,
        &mut completed,
        &mut visiting,
        &mut result,
        plugin_id,
    )?;
    Ok(result)
}

/// Enabled plugins transitively depending on `target`, intersected with `enabled`.
pub fn dependants(
    manifests: &BTreeMap<String, Manifest>,
    target: &str,
    enabled: &BTreeSet<String>,
) -> BTreeSet<String> {
    let mut affected: BTreeSet<String> = BTreeSet::from([target.to_string()]);
    let enabled_hosts: BTreeSet<&String> = enabled
        .iter()
        .filter(|name| {
            manifests
                .get(*name)
                .is_some_and(|manifest| manifest.role() == ROLE_HOST)
        })
        .collect();
    loop {
        let mut expanded = affected.clone();
        for name in enabled {
            if manifests.get(name).is_some_and(|manifest| {
                manifest
                    .requires_plugins
                    .iter()
                    .any(|dependency| affected.contains(dependency))
            }) {
                expanded.insert(name.clone());
            }
        }
        let touches_host = expanded.iter().any(|name| enabled_hosts.contains(name));
        let orphan_view =
            enabled.contains(target) && !manifests.contains_key(target) && enabled_hosts.is_empty();
        if touches_host || orphan_view {
            for name in enabled {
                if manifests
                    .get(name)
                    .is_some_and(|manifest| manifest.role() == ROLE_VIEW)
                {
                    expanded.insert(name.clone());
                }
            }
        }
        if expanded == affected {
            return expanded.intersection(enabled).cloned().collect();
        }
        affected = expanded;
    }
}

#[cfg(test)]
mod tests {
    use super::{dependants, dependency_order};
    use crate::catalog::{Manifest, Role};
    use anyhow::Result;
    use std::collections::{BTreeMap, BTreeSet};

    fn catalog(entries: &[(&str, Role, &[&str])]) -> BTreeMap<String, Manifest> {
        entries
            .iter()
            .map(|(id, role, dependencies)| {
                (
                    (*id).to_string(),
                    Manifest::test_manifest(id, *role, dependencies, &[]),
                )
            })
            .collect()
    }

    fn enabled(names: &[&str]) -> BTreeSet<String> {
        names.iter().map(ToString::to_string).collect()
    }

    fn ordered(manifests: &BTreeMap<String, Manifest>, id: &str) -> Result<Vec<String>> {
        dependency_order(manifests, id, &BTreeMap::new())
    }

    #[test]
    fn shared_dependency_comes_first_without_duplicates() -> Result<()> {
        let manifests = catalog(&[
            ("base", Role::View, &[]),
            ("left", Role::View, &["base"]),
            ("right", Role::View, &["base"]),
            ("app", Role::View, &["left", "right"]),
        ]);
        assert_eq!(
            ordered(&manifests, "app")?,
            vec!["base", "left", "right", "app"]
        );
        Ok(())
    }

    #[test]
    fn dependency_cycle_is_rejected() -> Result<()> {
        let manifests = catalog(&[("a", Role::View, &["b"]), ("b", Role::View, &["a"])]);
        match ordered(&manifests, "a") {
            Err(error) => assert!(error.to_string().contains("cycle"), "{error:?}"),
            Ok(order) => panic!("cycle accepted: {order:?}"),
        }
        Ok(())
    }

    #[test]
    fn missing_transitive_dependency_is_reported() -> Result<()> {
        let manifests = catalog(&[("app", Role::View, &["missing"])]);
        match ordered(&manifests, "app") {
            Err(error) => assert!(error.to_string().contains("missing"), "{error:?}"),
            Ok(order) => panic!("missing dependency accepted: {order:?}"),
        }
        Ok(())
    }

    #[test]
    fn invalid_manifest_diagnostic_is_surfaced() -> Result<()> {
        let manifests = catalog(&[("app", Role::View, &["broken"])]);
        let errors =
            BTreeMap::from([("broken".to_string(), "Invalid plugin manifest".to_string())]);
        match dependency_order(&manifests, "app", &errors) {
            Err(error) => assert!(
                error.to_string().contains("Invalid plugin manifest"),
                "{error:?}"
            ),
            Ok(order) => panic!("broken dependency accepted: {order:?}"),
        }
        Ok(())
    }

    #[test]
    fn disable_cascades_through_enabled_dependants() {
        let manifests = catalog(&[
            ("base", Role::View, &[]),
            ("mid", Role::View, &["base"]),
            ("app", Role::View, &["mid"]),
            ("other", Role::View, &[]),
        ]);
        let affected = dependants(
            &manifests,
            "base",
            &enabled(&["base", "mid", "app", "other"]),
        );
        assert_eq!(affected, enabled(&["base", "mid", "app"]));
    }

    #[test]
    fn uninstall_shared_dependency_keeps_sibling_branch() {
        let manifests = catalog(&[
            ("base", Role::View, &[]),
            ("left", Role::View, &["base"]),
            ("right", Role::View, &["base"]),
            ("app", Role::View, &["left", "right"]),
        ]);
        let all = enabled(&["base", "left", "right", "app"]);
        assert_eq!(
            dependants(&manifests, "left", &all),
            enabled(&["left", "app"])
        );
    }
}
