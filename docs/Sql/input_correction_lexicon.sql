-- Project configuration table for pre-execution input correction.
-- This is not a business-data table and does not change clothing_info.

CREATE TABLE IF NOT EXISTS vibe_input_correction_lexicon (
  correction_id VARCHAR(64) PRIMARY KEY,
  correct_word VARCHAR(255) NOT NULL,
  normalized_word VARCHAR(255) NOT NULL,
  enabled TINYINT(1) NOT NULL DEFAULT 1,
  note VARCHAR(500) NOT NULL DEFAULT '',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_input_correction_normalized (normalized_word),
  INDEX idx_input_correction_enabled_updated (enabled, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Example: the UI can add this record through /api/v1/input-corrections.
-- INSERT INTO vibe_input_correction_lexicon
--   (correction_id, correct_word, normalized_word, enabled, note)
-- VALUES
--   ('corr_thenorthface', 'thenorthface', 'thenorthface', 1, '人工确认的前端纠错正确写法');
