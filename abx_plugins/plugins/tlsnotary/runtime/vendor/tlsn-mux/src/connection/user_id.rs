use crate::error::ConnectionError;

/// A validated user-defined stream identifier.
///
/// User IDs must be 1-256 bytes in length.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub(crate) struct UserId(Vec<u8>);

impl UserId {
    /// Maximum length of a user ID in bytes.
    pub const MAX_LEN: usize = 256;

    /// Create a new validated UserId.
    ///
    /// Returns an error if the bytes are empty or exceed 256 bytes.
    pub fn new(bytes: impl Into<Vec<u8>>) -> Result<Self, ConnectionError> {
        let bytes = bytes.into();
        if bytes.is_empty() || bytes.len() > Self::MAX_LEN {
            return Err(ConnectionError::InvalidUserIdLength);
        }
        Ok(Self(bytes))
    }

    /// Returns the user ID as a byte slice.
    pub fn as_bytes(&self) -> &[u8] {
        &self.0
    }
}
