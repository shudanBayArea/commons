# ==================================================================================================
# Copyright 2011 Twitter, Inc.
# --------------------------------------------------------------------------------------------------
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this work except in compliance with the License.
# You may obtain a copy of the License in the LICENSE file, or at:
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==================================================================================================

from __future__ import print_function

from twitter.common.collections import OrderedSet
from twitter.pants import is_internal, is_jvm
from twitter.pants.base import ParseContext
from twitter.pants.targets import (
  AnnotationProcessor,
  InternalTarget,
  JavaLibrary,
  JavaProtobufLibrary,
  JavaTests,
  JavaThriftLibrary,
  ScalaLibrary,
  ScalaTests
)

def _aggregate(target_type, name, libs, deployjar=None, buildflags=None):
  all_deps = OrderedSet()
  all_excludes = OrderedSet()
  all_sources = []
  all_java_sources = []
  all_resources = []
  all_annotation_processors = []

  kwargs = dict(is_meta=True)

  if deployjar:
    kwargs['deployjar'] = deployjar

  if buildflags:
    kwargs['buildflags'] = buildflags

  for lib in libs:
    if lib.dependencies:
      all_deps.update(dep for dep in lib.jar_dependencies if dep.rev is not None)
      kwargs['dependencies'] = all_deps
    if lib.excludes:
      all_excludes.update(lib.excludes)
      kwargs['excludes'] = all_excludes
    if lib.sources:
      all_sources.extend(lib.sources)
    if hasattr(lib, 'java_sources') and lib.java_sources:
      all_java_sources.extend(lib.java_sources)
      kwargs['java_sources'] = all_java_sources
    if hasattr(lib, 'resources') and lib.resources:
      all_resources.extend(lib.resources)
      kwargs['resources'] = all_resources
    if hasattr(lib, 'processors') and lib.processors:
      all_annotation_processors.extend(lib.processors)
      kwargs['processors'] = all_annotation_processors

  return target_type(name, all_sources, **kwargs)


def extract_target(java_targets, name = None):
  """Extracts a minimal set of linked targets from the given target's internal transitive dependency
  set.  The root target in the extracted target set is returned.  The algorithm does a topological
  sort of the internal targets and then tries to coalesce targets of a given type.  Any target with
  a custom ant build xml will be excluded from the coalescing."""

  # TODO(John Sirois): this is broken - representative_target is not necessarily representative
  representative_target = list(java_targets)[0]

  meta_target_base_name = "fast-%s" % (name if name else representative_target.name)
  deployjar = hasattr(representative_target, 'deployjar') and representative_target.deployjar
  buildflags = representative_target.buildflags

  def discriminator(tgt):
    # Chunk up our targets by (type, src base) - the javac task in the ant build relies upon a
    # single srcdir that points to the root of a package tree to ensure differential compilation
    # works.
    return type(tgt), tgt.target_base

  def create_target(category, target_name, target_index, targets):
    def name(name):
      return "%s-%s-%d" % (target_name, name, target_index)

    # TODO(John Sirois): JavaLibrary and ScalaLibrary can float here between src/ and tests/ - add
    # ant build support to allow the same treatment for JavaThriftLibrary and JavaProtobufLibrary
    # so that tests can house test IDL in tests/
    target_type, base = category
    with ParseContext.temp(base):
      if target_type == JavaProtobufLibrary:
        return _aggregate(JavaProtobufLibrary, name('protobuf'), targets, buildflags=buildflags)
      elif target_type == JavaThriftLibrary:
        return _aggregate(JavaThriftLibrary, name('thrift'), targets, buildflags=buildflags)
      elif target_type == AnnotationProcessor:
        return _aggregate(AnnotationProcessor, name('apt'), targets)
      elif target_type == JavaLibrary:
        return _aggregate(JavaLibrary, name('java'), targets, deployjar, buildflags)
      elif target_type == ScalaLibrary:
        return _aggregate(ScalaLibrary, name('scala'), targets, deployjar, buildflags)
      elif target_type == JavaTests:
        return _aggregate(JavaTests, name('java-tests'), targets, buildflags=buildflags)
      elif target_type == ScalaTests:
        return _aggregate(ScalaTests, name('scala-tests'), targets, buildflags=buildflags)
      else:
        raise Exception("Cannot aggregate targets of type: %s" % target_type)

  # TODO(John Sirois): support a flag that selects conflict resolution policy - this currently
  # happens to mirror the ivy policy we use
  def resolve_conflicts(target):
    dependencies = {}
    for dependency in target.dependencies:
      for jar in dependency._as_jar_dependencies():
        key = jar.org, jar.name
        previous = dependencies.get(key, jar)
        if jar.rev >= previous.rev:
          if jar != previous:
            print("WARNING: replacing %s with %s for %s" % (previous, jar, target.id))
            target.dependencies.remove(previous)
            target.jar_dependencies.remove(previous)
          dependencies[key] = jar
    return target

  # chunk up our targets by type & custom build xml
  coalesced = InternalTarget.coalesce_targets(java_targets, discriminator)
  coalesced = list(reversed(coalesced))

  start_type = discriminator(coalesced[0])
  start = 0
  descriptors = []

  for current in range(0, len(coalesced)):
    current_target = coalesced[current]
    current_type = discriminator(current_target)

    if current_target.custom_antxml_path:
      if start < current:
        # if we have a type chunk to our left, record it
        descriptors.append((start_type, coalesced[start:current]))

      # record a chunk containing just the target that has the custom build xml to be conservative
      descriptors.append((current_type, [current_target]))
      start = current + 1
      if current < (len(coalesced) - 1):
        start_type = discriminator(coalesced[start])

    elif start_type != current_type:
      # record the type chunk we just left
      descriptors.append((start_type, coalesced[start:current]))
      start = current
      start_type = current_type

  if start < len(coalesced):
    # record the tail chunk
    descriptors.append((start_type, coalesced[start:]))

  # build meta targets aggregated from the chunks and keep track of which targets end up in which
  # meta targets
  meta_targets_by_target_id = dict()
  targets_by_meta_target = []
  for (ttype, targets), index in zip(descriptors, reversed(range(0, len(descriptors)))):
    meta_target = resolve_conflicts(create_target(ttype, meta_target_base_name, index, targets))
    targets_by_meta_target.append((meta_target, targets))
    for target in targets:
      meta_targets_by_target_id[target.id] = meta_target

  # calculate the other meta-targets (if any) each meta-target depends on
  extra_targets_by_meta_target = []
  for meta_target, targets in targets_by_meta_target:
    meta_deps = set()
    custom_antxml_path = None
    for target in targets:
      if target.custom_antxml_path:
        custom_antxml_path = target.custom_antxml_path
      for dep in target.dependencies:
        if is_jvm(dep):
          meta = meta_targets_by_target_id[dep.id]
          if meta != meta_target:
            meta_deps.add(meta)
    extra_targets_by_meta_target.append((meta_target, meta_deps, custom_antxml_path))

  def lift_excludes(meta_target):
    excludes = set()
    def lift(target):
      if target.excludes:
        excludes.update(target.excludes)
      for jar_dep in target.jar_dependencies:
        excludes.update(jar_dep.excludes)
    meta_target.walk(lift, is_internal)
    return excludes

  # link in the extra inter-meta deps
  meta_targets = []
  for meta_target, extra_deps, custom_antxml_path in extra_targets_by_meta_target:
    meta_targets.append(meta_target)
    meta_target.update_dependencies(extra_deps)
    meta_target.excludes = lift_excludes(meta_target)
    meta_target.custom_antxml_path = custom_antxml_path

  sorted_meta_targets = InternalTarget.sort_targets(meta_targets)
  def prune_metas(target):
    if sorted_meta_targets:
      try:
        sorted_meta_targets.remove(target)
      except ValueError:
        # we've already removed target in the current walk
        pass

  # link any disconnected meta_target graphs so we can return 1 root target
  root = None
  while sorted_meta_targets:
    new_root = sorted_meta_targets[0]
    new_root.walk(prune_metas, is_jvm)
    if root:
      new_root.update_dependencies([root])
    root = new_root

  return root
