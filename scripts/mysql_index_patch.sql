-- Index patch for dataservice_test_local snapshot tables
ALTER TABLE clothing_info
  ADD INDEX idx_clothing_info_id (Id),
  ADD INDEX idx_clothing_info_brand (BrandName),
  ADD INDEX idx_clothing_info_category (Category, SubCategory, LeafCategory),
  ADD INDEX idx_clothing_info_price (Price);

ALTER TABLE clothing_fiber_info
  ADD INDEX idx_clothing_fiber_info_clothing_id (ClothingId),
  ADD INDEX idx_clothing_fiber_info_name (Name);

ALTER TABLE clothing_functions_info
  ADD INDEX idx_clothing_functions_info_clothing_id (ClothingId);

ALTER TABLE clothing_images_color
  ADD INDEX idx_clothing_images_color_clothing_id (ClothingId),
  ADD INDEX idx_clothing_images_color_coloro_id (ColoroId);

ALTER TABLE clothing_pattern_info
  ADD INDEX idx_clothing_pattern_info_clothing_id (ClothingId),
  ADD INDEX idx_clothing_pattern_info_pattern (pattern);

ALTER TABLE clothing_scene_info
  ADD INDEX idx_clothing_scene_info_clothing_id (ClothingId),
  ADD INDEX idx_clothing_scene_info_scene (Scene);

ALTER TABLE clothing_texture_info
  ADD INDEX idx_clothing_texture_info_clothing_id (ClothingId),
  ADD INDEX idx_clothing_texture_info_texture (Texture);
