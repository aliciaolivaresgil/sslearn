# Change Log
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.2] - 2023-02-17

### Fixed

- Fixed a bug in TriTraining when one of the base estimators has not a random_state parameter.
- Fixed OneVsRestSSL with the random_state parameter.
- Fixed WiWTriTraining when no `instance_group` parameter is not provided.
- Fixed a FutureWarning for `sparse` parameter in `OneHotEncoder`. Changed to `sparse_output`.

## [1.0.1] - 2023-02-10

### Added

- CoTraining support a `threshold` parameter (default to 0.5) to control the threshold for adding new instances in the next iteration.

### Fixed

- Fixed a bug in CoTraining using LabelEncoder.