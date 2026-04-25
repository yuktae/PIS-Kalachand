-- ============================================================
-- PostgreSQL Audit Trigger for PIS System
-- Automatically logs field-level changes to pis_data & spec_data
-- into the field_change_log table as a safety net.
-- ============================================================

-- Create or replace the trigger function
CREATE OR REPLACE FUNCTION audit_product_jsonb_changes()
RETURNS TRIGGER AS $$
DECLARE
    _field_name TEXT;
    _old_val TEXT;
    _new_val TEXT;
    _key TEXT;
BEGIN
    -- ---- Track pis_data changes ----
    IF OLD.pis_data IS DISTINCT FROM NEW.pis_data THEN
        -- Log top-level header_info changes
        IF (OLD.pis_data->'header_info') IS DISTINCT FROM (NEW.pis_data->'header_info') THEN
            FOR _key IN SELECT jsonb_object_keys(
                COALESCE(NEW.pis_data->'header_info', '{}'::jsonb) ||
                COALESCE(OLD.pis_data->'header_info', '{}'::jsonb)
            ) LOOP
                _old_val := OLD.pis_data->'header_info'->>_key;
                _new_val := NEW.pis_data->'header_info'->>_key;
                IF _old_val IS DISTINCT FROM _new_val AND _old_val IS NOT NULL THEN
                    INSERT INTO field_change_log (product_id, field_name, old_value, new_value, timestamp)
                    VALUES (NEW.id, 'pis_data.header_info.' || _key, 
                            LEFT(_old_val, 2000), LEFT(_new_val, 2000), NOW());
                END IF;
            END LOOP;
        END IF;

        -- Log range_overview (description) changes
        IF (OLD.pis_data->>'range_overview') IS DISTINCT FROM (NEW.pis_data->>'range_overview') 
           AND (OLD.pis_data->>'range_overview') IS NOT NULL THEN
            INSERT INTO field_change_log (product_id, field_name, old_value, new_value, timestamp)
            VALUES (NEW.id, 'pis_data.range_overview',
                    LEFT(OLD.pis_data->>'range_overview', 2000),
                    LEFT(NEW.pis_data->>'range_overview', 2000), NOW());
        END IF;

        -- Log warranty changes
        IF (OLD.pis_data->'warranty_service') IS DISTINCT FROM (NEW.pis_data->'warranty_service') THEN
            FOR _key IN SELECT jsonb_object_keys(
                COALESCE(NEW.pis_data->'warranty_service', '{}'::jsonb) ||
                COALESCE(OLD.pis_data->'warranty_service', '{}'::jsonb)
            ) LOOP
                _old_val := OLD.pis_data->'warranty_service'->>_key;
                _new_val := NEW.pis_data->'warranty_service'->>_key;
                IF _old_val IS DISTINCT FROM _new_val AND _old_val IS NOT NULL THEN
                    INSERT INTO field_change_log (product_id, field_name, old_value, new_value, timestamp)
                    VALUES (NEW.id, 'pis_data.warranty_service.' || _key,
                            LEFT(_old_val, 2000), LEFT(_new_val, 2000), NOW());
                END IF;
            END LOOP;
        END IF;

        -- Log SEO data changes
        IF (OLD.pis_data->'seo_data') IS DISTINCT FROM (NEW.pis_data->'seo_data') THEN
            FOR _key IN SELECT jsonb_object_keys(
                COALESCE(NEW.pis_data->'seo_data', '{}'::jsonb) ||
                COALESCE(OLD.pis_data->'seo_data', '{}'::jsonb)
            ) LOOP
                _old_val := OLD.pis_data->'seo_data'->>_key;
                _new_val := NEW.pis_data->'seo_data'->>_key;
                IF _old_val IS DISTINCT FROM _new_val AND _old_val IS NOT NULL THEN
                    INSERT INTO field_change_log (product_id, field_name, old_value, new_value, timestamp)
                    VALUES (NEW.id, 'pis_data.seo_data.' || _key,
                            LEFT(_old_val, 2000), LEFT(_new_val, 2000), NOW());
                END IF;
            END LOOP;
        END IF;
    END IF;

    -- ---- Track spec_data changes ----
    IF OLD.spec_data IS DISTINCT FROM NEW.spec_data THEN
        -- Log spec header_info changes
        IF (OLD.spec_data->'header_info') IS DISTINCT FROM (NEW.spec_data->'header_info') THEN
            FOR _key IN SELECT jsonb_object_keys(
                COALESCE(NEW.spec_data->'header_info', '{}'::jsonb) ||
                COALESCE(OLD.spec_data->'header_info', '{}'::jsonb)
            ) LOOP
                _old_val := OLD.spec_data->'header_info'->>_key;
                _new_val := NEW.spec_data->'header_info'->>_key;
                IF _old_val IS DISTINCT FROM _new_val AND _old_val IS NOT NULL THEN
                    INSERT INTO field_change_log (product_id, field_name, old_value, new_value, timestamp)
                    VALUES (NEW.id, 'spec_data.header_info.' || _key,
                            LEFT(_old_val, 2000), LEFT(_new_val, 2000), NOW());
                END IF;
            END LOOP;
        END IF;

        -- Log customer_friendly_description changes
        IF (OLD.spec_data->>'customer_friendly_description') IS DISTINCT FROM (NEW.spec_data->>'customer_friendly_description')
           AND (OLD.spec_data->>'customer_friendly_description') IS NOT NULL THEN
            INSERT INTO field_change_log (product_id, field_name, old_value, new_value, timestamp)
            VALUES (NEW.id, 'spec_data.customer_friendly_description',
                    LEFT(OLD.spec_data->>'customer_friendly_description', 2000),
                    LEFT(NEW.spec_data->>'customer_friendly_description', 2000), NOW());
        END IF;

        -- Log SEO changes
        IF (OLD.spec_data->'seo') IS DISTINCT FROM (NEW.spec_data->'seo') THEN
            FOR _key IN SELECT jsonb_object_keys(
                COALESCE(NEW.spec_data->'seo', '{}'::jsonb) ||
                COALESCE(OLD.spec_data->'seo', '{}'::jsonb)
            ) LOOP
                _old_val := OLD.spec_data->'seo'->>_key;
                _new_val := NEW.spec_data->'seo'->>_key;
                IF _old_val IS DISTINCT FROM _new_val AND _old_val IS NOT NULL THEN
                    INSERT INTO field_change_log (product_id, field_name, old_value, new_value, timestamp)
                    VALUES (NEW.id, 'spec_data.seo.' || _key,
                            LEFT(_old_val, 2000), LEFT(_new_val, 2000), NOW());
                END IF;
            END LOOP;
        END IF;

        -- Log category changes
        IF (OLD.spec_data->'categories') IS DISTINCT FROM (NEW.spec_data->'categories') THEN
            FOR _key IN SELECT jsonb_object_keys(
                COALESCE(NEW.spec_data->'categories', '{}'::jsonb) ||
                COALESCE(OLD.spec_data->'categories', '{}'::jsonb)
            ) LOOP
                _old_val := OLD.spec_data->'categories'->>_key;
                _new_val := NEW.spec_data->'categories'->>_key;
                IF _old_val IS DISTINCT FROM _new_val AND _old_val IS NOT NULL THEN
                    INSERT INTO field_change_log (product_id, field_name, old_value, new_value, timestamp)
                    VALUES (NEW.id, 'spec_data.categories.' || _key,
                            LEFT(_old_val, 2000), LEFT(_new_val, 2000), NOW());
                END IF;
            END LOOP;
        END IF;

        -- Log warranty changes in spec_data
        IF (OLD.spec_data->'warranty_service') IS DISTINCT FROM (NEW.spec_data->'warranty_service') THEN
            FOR _key IN SELECT jsonb_object_keys(
                COALESCE(NEW.spec_data->'warranty_service', '{}'::jsonb) ||
                COALESCE(OLD.spec_data->'warranty_service', '{}'::jsonb)
            ) LOOP
                _old_val := OLD.spec_data->'warranty_service'->>_key;
                _new_val := NEW.spec_data->'warranty_service'->>_key;
                IF _old_val IS DISTINCT FROM _new_val AND _old_val IS NOT NULL THEN
                    INSERT INTO field_change_log (product_id, field_name, old_value, new_value, timestamp)
                    VALUES (NEW.id, 'spec_data.warranty_service.' || _key,
                            LEFT(_old_val, 2000), LEFT(_new_val, 2000), NOW());
                END IF;
            END LOOP;
        END IF;

        -- Log internal_web_keywords changes
        IF (OLD.spec_data->>'internal_web_keywords') IS DISTINCT FROM (NEW.spec_data->>'internal_web_keywords')
           AND (OLD.spec_data->>'internal_web_keywords') IS NOT NULL THEN
            INSERT INTO field_change_log (product_id, field_name, old_value, new_value, timestamp)
            VALUES (NEW.id, 'spec_data.internal_web_keywords',
                    LEFT(OLD.spec_data->>'internal_web_keywords', 2000),
                    LEFT(NEW.spec_data->>'internal_web_keywords', 2000), NOW());
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Drop existing trigger if any (idempotent re-install)
DROP TRIGGER IF EXISTS trg_audit_product_jsonb ON product;

-- Create the trigger — fires on every UPDATE of pis_data or spec_data
CREATE TRIGGER trg_audit_product_jsonb
    AFTER UPDATE OF pis_data, spec_data ON product
    FOR EACH ROW
    EXECUTE FUNCTION audit_product_jsonb_changes();
