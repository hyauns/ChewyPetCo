<?php
/**
 * Plugin Name: Chewy Product Specs for GraphQL/REST
 * Description: Exposes the Chewy product spec meta (written by import_woocommerce_api.py)
 *              on the WooGraphQL Product / ProductVariation types and the WP REST API,
 *              so the headless Next.js frontend can read specs WITHOUT re-normalizing.
 * Author: ChewyPetCo
 * Version: 1.0
 *
 * INSTALL: drop this file into  wp-content/mu-plugins/  (create the folder if needed).
 *          mu-plugins load automatically — no activation required.
 *
 * The importer stores these meta keys (canonical, consistent across all products):
 *   ingredients, guaranteed_analysis, feeding_instructions, nutrition, specifications  (JSON strings)
 *   pet_type, food_form, life_stage, breed_size, pet_weight, flavor, special_diet,
 *   package_type, color, scent, brand, source_url, source_product_id                   (scalar strings)
 *
 * GraphQL field names are camelCase (WPGraphQL convention), e.g.:
 *   query { product(id:"..", idType:DATABASE_ID) {
 *     ... on SimpleProduct { ingredients guaranteedAnalysis petType brand specs }
 *   }}
 * The JSON fields (ingredients, guaranteedAnalysis, nutrition, specifications,
 * feedingInstructions) are returned as JSON strings -> JSON.parse() on the frontend.
 * `specs` returns ALL spec keys as one JSON object string (future-proof: new keys
 * appear automatically without touching the frontend query).
 */

if (!defined('ABSPATH')) {
    exit;
}

/**
 * Canonical spec meta keys. Keep in sync with KEY_MAP/SPEC_CANON in
 * tools/import_woocommerce_api.py.
 */
function chewy_spec_meta_keys() {
    return array(
        'ingredients', 'guaranteed_analysis', 'feeding_instructions', 'nutrition',
        'specifications', 'pet_type', 'food_form', 'life_stage', 'breed_size',
        'pet_weight', 'flavor', 'special_diet', 'package_type', 'color', 'scent',
        'brand', 'source_url', 'source_product_id',
    );
}

/** snake_case -> camelCase for GraphQL field names. */
function chewy_camel($key) {
    $parts = explode('_', $key);
    $out = array_shift($parts);
    foreach ($parts as $p) {
        $out .= ucfirst($p);
    }
    return $out;
}

/** Resolve the underlying post ID from a WooGraphQL model. */
function chewy_resolve_post_id($source) {
    if (is_object($source)) {
        if (isset($source->ID) && $source->ID) {
            return (int) $source->ID;
        }
        if (isset($source->databaseId) && $source->databaseId) {
            return (int) $source->databaseId;
        }
    }
    return 0;
}

/* ---------------------------------------------------------------------------
 * 1) Expose via WPGraphQL / WooGraphQL
 * ------------------------------------------------------------------------- */
add_action('graphql_register_types', function () {
    // Concrete WooGraphQL types that should carry the spec fields.
    $types = array(
        'SimpleProduct', 'VariableProduct', 'ExternalProduct', 'GroupProduct',
        'ProductVariation',
    );
    $keys = chewy_spec_meta_keys();

    foreach ($types as $type) {
        // Individual fields (camelCase), each returns the raw stored meta string.
        foreach ($keys as $key) {
            register_graphql_field($type, chewy_camel($key), array(
                'type'        => 'String',
                'description' => "Chewy spec meta '{$key}'. JSON-typed specs are JSON strings.",
                'resolve'     => function ($source) use ($key) {
                    $id = chewy_resolve_post_id($source);
                    if (!$id) {
                        return null;
                    }
                    $v = get_post_meta($id, $key, true);
                    return ($v === '' || $v === false) ? null : (string) $v;
                },
            ));
        }

        // One JSON blob of every present spec key (future-proof for the frontend).
        register_graphql_field($type, 'specs', array(
            'type'        => 'String',
            'description' => 'All Chewy spec meta as a single JSON object string.',
            'resolve'     => function ($source) use ($keys) {
                $id = chewy_resolve_post_id($source);
                if (!$id) {
                    return null;
                }
                $out = array();
                foreach ($keys as $key) {
                    $v = get_post_meta($id, $key, true);
                    if ($v !== '' && $v !== false && $v !== null) {
                        $out[$key] = $v;
                    }
                }
                return wp_json_encode($out);
            },
        ));
    }
});

/* ---------------------------------------------------------------------------
 * 2) Also expose on the WP REST API (wp/v2 + Store API) for good measure.
 *    (The authenticated wc/v3 API already returns these in meta_data.)
 * ------------------------------------------------------------------------- */
add_action('init', function () {
    foreach (chewy_spec_meta_keys() as $key) {
        register_post_meta('product', $key, array(
            'show_in_rest' => true,
            'single'       => true,
            'type'         => 'string',
            'auth_callback' => '__return_true',
        ));
        register_post_meta('product_variation', $key, array(
            'show_in_rest' => true,
            'single'       => true,
            'type'         => 'string',
            'auth_callback' => '__return_true',
        ));
    }
});
