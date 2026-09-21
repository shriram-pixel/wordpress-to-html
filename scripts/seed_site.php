<?php
/**
 * Install WordPress and populate it with representative content.
 *
 * Run by scripts/make_fixture.py to build a realistic .wpress test archive:
 * a site with pages, posts, categories, tags, a menu, images and a real theme,
 * so the conversion pipeline is exercised against genuine WordPress output
 * rather than hand-written HTML.
 *
 * Usage: php seed_site.php <wordpress-root> <site-url>
 */

if ( PHP_SAPI !== 'cli' ) {
    exit( 'CLI only' );
}

list( , $wp_root, $site_url ) = array_pad( $argv, 3, null );
if ( ! $wp_root || ! $site_url ) {
    fwrite( STDERR, "usage: php seed_site.php <wordpress-root> <site-url>\n" );
    exit( 1 );
}

define( 'WP_INSTALLING', true );
define( 'WP_USE_THEMES', false );

require_once rtrim( $wp_root, '/\\' ) . '/wp-load.php';
require_once ABSPATH . 'wp-admin/includes/upgrade.php';
require_once ABSPATH . 'wp-admin/includes/image.php';
require_once ABSPATH . 'wp-admin/includes/file.php';
require_once ABSPATH . 'wp-admin/includes/media.php';

function say( $message ) {
    fwrite( STDOUT, $message . "\n" );
}

/* -------------------------------------------------------------------------
 * Install
 * ---------------------------------------------------------------------- */
if ( ! is_blog_installed() ) {
    $result = wp_install(
        'Northwind Studio',
        'demoadmin',
        'demo@example.test',
        true,               // public
        '',
        wp_generate_password( 20 )
    );
    say( 'installed WordPress, admin user id ' . $result['user_id'] );
} else {
    say( 'WordPress already installed' );
}

update_option( 'siteurl', $site_url );
update_option( 'home', $site_url );
update_option( 'blogdescription', 'Design and build studio' );
update_option( 'permalink_structure', '/%postname%/' );
update_option( 'posts_per_page', 3 );   // small, so pagination is exercised
update_option( 'timezone_string', 'UTC' );

$wp_rewrite->set_permalink_structure( '/%postname%/' );
$wp_rewrite->flush_rules( true );

/* -------------------------------------------------------------------------
 * Media: register the pre-generated images as real attachments
 * ---------------------------------------------------------------------- */
function seed_attachment( $filename, $title ) {
    $uploads = wp_upload_dir();
    $path    = $uploads['path'] . '/' . $filename;

    if ( ! file_exists( $path ) ) {
        say( "  ! missing image $path" );
        return 0;
    }

    $existing = get_page_by_path( sanitize_title( $title ), OBJECT, 'attachment' );
    if ( $existing ) {
        return $existing->ID;
    }

    $attachment_id = wp_insert_attachment(
        array(
            'post_mime_type' => 'image/png',
            'post_title'     => $title,
            'post_content'   => '',
            'post_status'    => 'inherit',
        ),
        $path
    );

    // Generates the resized variants, which is what produces srcset markup.
    $metadata = wp_generate_attachment_metadata( $attachment_id, $path );
    wp_update_attachment_metadata( $attachment_id, $metadata );

    return $attachment_id;
}

$hero_id    = seed_attachment( 'hero.png', 'Hero image' );
$gallery_a  = seed_attachment( 'work-a.png', 'Work A' );
$gallery_b  = seed_attachment( 'work-b.png', 'Work B' );
$logo_id    = seed_attachment( 'logo.png', 'Studio logo' );

say( "attachments: hero=$hero_id a=$gallery_a b=$gallery_b logo=$logo_id" );

/* -------------------------------------------------------------------------
 * Taxonomies
 * ---------------------------------------------------------------------- */
$categories = array();
foreach ( array( 'Studio News', 'Case Studies', 'Process' ) as $name ) {
    $term = term_exists( $name, 'category' );
    if ( ! $term ) {
        $term = wp_insert_term( $name, 'category' );
    }
    if ( ! is_wp_error( $term ) ) {
        $categories[ $name ] = (int) $term['term_id'];
    }
}

foreach ( array( 'branding', 'typography', 'accessibility' ) as $tag ) {
    if ( ! term_exists( $tag, 'post_tag' ) ) {
        wp_insert_term( $tag, 'post_tag' );
    }
}

/* -------------------------------------------------------------------------
 * Pages
 * ---------------------------------------------------------------------- */
function seed_page( $title, $slug, $content, $parent = 0 ) {
    $existing = get_page_by_path( $slug );
    if ( $existing ) {
        return $existing->ID;
    }
    return wp_insert_post(
        array(
            'post_title'   => $title,
            'post_name'    => $slug,
            'post_content' => $content,
            'post_status'  => 'publish',
            'post_type'    => 'page',
            'post_parent'  => $parent,
        )
    );
}

$hero_url = $hero_id ? wp_get_attachment_url( $hero_id ) : '';

$home_content = '
<!-- wp:heading {"level":1} --><h1 class="wp-block-heading">We design calm, durable brands</h1><!-- /wp:heading -->
<!-- wp:paragraph --><p>Northwind Studio is a small team working on identity, typography and accessible interfaces.</p><!-- /wp:paragraph -->
' . ( $hero_id ? '<!-- wp:image {"id":' . $hero_id . ',"sizeSlug":"large"} --><figure class="wp-block-image size-large"><img src="' . esc_url( $hero_url ) . '" alt="Studio workspace" class="wp-image-' . $hero_id . '"/></figure><!-- /wp:image -->' : '' ) . '
<!-- wp:buttons --><div class="wp-block-buttons"><!-- wp:button --><div class="wp-block-button"><a class="wp-block-button__link wp-element-button" href="' . esc_url( $site_url ) . '/contact/">Start a project</a></div><!-- /wp:button --></div><!-- /wp:buttons -->
<!-- wp:columns --><div class="wp-block-columns">
<!-- wp:column --><div class="wp-block-column"><!-- wp:heading {"level":3} --><h3 class="wp-block-heading">Identity</h3><!-- /wp:heading --><!-- wp:paragraph --><p>Marks, systems and guidelines that survive contact with the real world.</p><!-- /wp:paragraph --></div><!-- /wp:column -->
<!-- wp:column --><div class="wp-block-column"><!-- wp:heading {"level":3} --><h3 class="wp-block-heading">Interfaces</h3><!-- /wp:heading --><!-- wp:paragraph --><p>Accessible, fast and quiet interfaces for products people use daily.</p><!-- /wp:paragraph --></div><!-- /wp:column -->
</div><!-- /wp:columns -->';

$home_id = seed_page( 'Home', 'home', $home_content );

$about_content = '
<!-- wp:heading {"level":1} --><h1 class="wp-block-heading">About the studio</h1><!-- /wp:heading -->
<!-- wp:paragraph --><p>Founded in 2014, we work with cultural institutions and small software teams.</p><!-- /wp:paragraph -->
<!-- wp:list --><ul class="wp-block-list"><li>Brand identity</li><li>Design systems</li><li>Editorial typography</li></ul><!-- /wp:list -->
<!-- wp:quote --><blockquote class="wp-block-quote"><p>Good design is as little design as possible.</p><cite>Dieter Rams</cite></blockquote><!-- /wp:quote -->';
$about_id = seed_page( 'About', 'about', $about_content );

$services_content = '
<!-- wp:heading {"level":1} --><h1 class="wp-block-heading">Services</h1><!-- /wp:heading -->
<!-- wp:paragraph --><p>Three ways we usually work together.</p><!-- /wp:paragraph -->
<!-- wp:table --><figure class="wp-block-table"><table><thead><tr><th>Engagement</th><th>Duration</th></tr></thead><tbody><tr><td>Identity sprint</td><td>3 weeks</td></tr><tr><td>Design system</td><td>8 weeks</td></tr><tr><td>Retainer</td><td>Ongoing</td></tr></tbody></table></figure><!-- /wp:table -->';
$services_id = seed_page( 'Services', 'services', $services_content );

$contact_content = '
<!-- wp:heading {"level":1} --><h1 class="wp-block-heading">Contact</h1><!-- /wp:heading -->
<!-- wp:paragraph --><p>Tell us about the project.</p><!-- /wp:paragraph -->
<!-- wp:html -->
<form class="demo-contact-form" method="post" action="' . esc_url( $site_url ) . '/wp-admin/admin-post.php">
  <input type="hidden" name="action" value="demo_contact" />
  <p><label for="cf-name">Name</label><br /><input id="cf-name" type="text" name="name" required /></p>
  <p><label for="cf-email">Email</label><br /><input id="cf-email" type="email" name="email" required /></p>
  <p><label for="cf-msg">Message</label><br /><textarea id="cf-msg" name="message" rows="5"></textarea></p>
  <p><button type="submit">Send message</button></p>
</form>
<!-- /wp:html -->';
$contact_id = seed_page( 'Contact', 'contact', $contact_content );

$blog_id = seed_page( 'Journal', 'journal', '' );

// A child page, so nested output paths are exercised.
seed_page( 'Accessibility statement', 'accessibility', '<!-- wp:paragraph --><p>We target WCAG 2.2 AA.</p><!-- /wp:paragraph -->', $about_id );

update_option( 'show_on_front', 'page' );
update_option( 'page_on_front', $home_id );
update_option( 'page_for_posts', $blog_id );

say( "pages: home=$home_id about=$about_id services=$services_id contact=$contact_id journal=$blog_id" );

/* -------------------------------------------------------------------------
 * Posts: enough to force pagination at 3 per page
 * ---------------------------------------------------------------------- */
$posts = array(
    array( 'Choosing a typeface for long reads', 'Studio News', array( 'typography' ) ),
    array( 'A colour system that survives dark mode', 'Process', array( 'branding', 'accessibility' ) ),
    array( 'Rebuilding a museum website', 'Case Studies', array( 'accessibility' ) ),
    array( 'What we learned shipping a design system', 'Process', array( 'branding' ) ),
    array( 'Notes on accessible forms', 'Studio News', array( 'accessibility' ) ),
    array( 'Print is not dead, it is just quieter', 'Studio News', array( 'typography' ) ),
    array( 'Designing for slow connections', 'Process', array( 'accessibility' ) ),
);

$index = 0;
foreach ( $posts as $entry ) {
    list( $title, $category, $tags ) = $entry;
    $slug = sanitize_title( $title );

    if ( get_page_by_path( $slug, OBJECT, 'post' ) ) {
        continue;
    }

    $body = '
<!-- wp:paragraph --><p><strong>' . esc_html( $title ) . '</strong> — a short note from the studio about how we approach this problem in practice.</p><!-- /wp:paragraph -->
<!-- wp:heading {"level":2} --><h2 class="wp-block-heading">Background</h2><!-- /wp:heading -->
<!-- wp:paragraph --><p>Every project starts with reading. We collect the constraints before we draw anything, and we write them down where the whole team can see them.</p><!-- /wp:paragraph -->
' . ( $gallery_a ? '<!-- wp:image {"id":' . $gallery_a . ',"sizeSlug":"large"} --><figure class="wp-block-image size-large"><img src="' . esc_url( wp_get_attachment_url( $gallery_a ) ) . '" alt="Work sample" class="wp-image-' . $gallery_a . '"/></figure><!-- /wp:image -->' : '' ) . '
<!-- wp:heading {"level":2} --><h2 class="wp-block-heading">What we changed</h2><!-- /wp:heading -->
<!-- wp:paragraph --><p>The result was fewer components, clearer naming and a page that loads in under a second on a slow connection. Read more on our <a href="' . esc_url( $site_url ) . '/about/">about page</a>.</p><!-- /wp:paragraph -->';

    $post_id = wp_insert_post(
        array(
            'post_title'   => $title,
            'post_name'    => $slug,
            'post_content' => $body,
            'post_status'  => 'publish',
            'post_type'    => 'post',
            'post_date'    => gmdate( 'Y-m-d H:i:s', strtotime( "-{$index} weeks" ) ),
        )
    );

    if ( $post_id && ! is_wp_error( $post_id ) ) {
        if ( isset( $categories[ $category ] ) ) {
            wp_set_post_categories( $post_id, array( $categories[ $category ] ) );
        }
        wp_set_post_tags( $post_id, $tags );
        if ( $gallery_b ) {
            set_post_thumbnail( $post_id, $gallery_b );
        }
    }
    $index++;
}
say( 'created ' . count( $posts ) . ' posts' );

/* -------------------------------------------------------------------------
 * Navigation menu
 * ---------------------------------------------------------------------- */
$menu_name = 'Primary';
$menu = wp_get_nav_menu_object( $menu_name );
if ( ! $menu ) {
    $menu_id = wp_create_nav_menu( $menu_name );

    foreach ( array(
        array( 'Home', $home_id ),
        array( 'About', $about_id ),
        array( 'Services', $services_id ),
        array( 'Journal', $blog_id ),
        array( 'Contact', $contact_id ),
    ) as $item ) {
        wp_update_nav_menu_item( $menu_id, 0, array(
            'menu-item-title'     => $item[0],
            'menu-item-object'    => 'page',
            'menu-item-object-id' => $item[1],
            'menu-item-type'      => 'post_type',
            'menu-item-status'    => 'publish',
        ) );
    }

    wp_update_nav_menu_item( $menu_id, 0, array(
        'menu-item-title'  => 'WordPress.org',
        'menu-item-url'    => 'https://wordpress.org/',
        'menu-item-type'   => 'custom',
        'menu-item-status' => 'publish',
    ) );

    $locations = get_registered_nav_menus();
    if ( $locations ) {
        $assignment = array();
        foreach ( array_keys( $locations ) as $location ) {
            $assignment[ $location ] = $menu_id;
        }
        set_theme_mod( 'nav_menu_locations', $assignment );
    }
    say( "created menu $menu_id and assigned it to " . count( get_registered_nav_menus() ) . ' location(s)' );
}

/* Site logo and icon exercise theme_mods, which are serialized values. */
if ( $logo_id ) {
    set_theme_mod( 'custom_logo', $logo_id );
    update_option( 'site_icon', $logo_id );
}

/* A serialized option carrying the site URL, to prove the serialized-safe
   replacement works on real data rather than only in unit tests. */
update_option( 'wpsc_demo_serialized', array(
    'home'     => $site_url,
    'endpoint' => $site_url . '/wp-json/demo/v1/thing',
    'nested'   => array( 'logo' => $hero_url, 'count' => 7 ),
) );

/* A custom post type, registered by the demo plugin below. */
foreach ( array( 'Aurora Rebrand', 'Harbour Wayfinding' ) as $project ) {
    $slug = sanitize_title( $project );
    if ( ! get_page_by_path( $slug, OBJECT, 'project' ) ) {
        wp_insert_post( array(
            'post_title'   => $project,
            'post_name'    => $slug,
            'post_content' => '<!-- wp:paragraph --><p>Case study: ' . esc_html( $project ) . '.</p><!-- /wp:paragraph -->',
            'post_status'  => 'publish',
            'post_type'    => 'project',
        ) );
    }
}

flush_rewrite_rules( true );
say( 'done' );
