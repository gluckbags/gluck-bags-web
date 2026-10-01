"""The catalogue of every editable string on the site.

This module is the single source of truth for BOTH the copy the site renders and
the fields the admin lists. A string is editable exactly when it has an entry here,
and its `default` is the copy the site shipped with — so an empty database renders
the site as the code always did, and "restore the original" is just dropping the
override row (see app/models/site_text.py).

Adding new copy is therefore one entry here plus one `t('<key>')` call in the
template. Nothing else — no migration, no seed, no admin form to extend.

Keys are dotted, stable identifiers: they are the database primary key, so renaming
one silently drops the editor's text. Treat them as an API.

Structure: Group (one admin screen + one preview target) -> Section (a card in that
screen) -> TextField.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FieldType = Literal["line", "text", "lines", "rich", "url", "image", "video"]

# Default cap per type. Long enough for real copy, short enough that a paste
# accident can't push a 1MB blob into every page render. `image`/`video` hold a
# location (a URL or a /static path), not the bytes — roomy for signed CDN links.
DEFAULT_MAX_LENGTH: dict[str, int] = {
    "line": 200,
    "text": 800,
    "lines": 600,
    "rich": 12000,
    "url": 300,
    "image": 500,
    "video": 500,
}


@dataclass(frozen=True)
class TextField:
    key: str
    label: str
    default: str
    type: FieldType = "line"
    hint: str = ""
    max_length: int = 0

    def __post_init__(self) -> None:
        if not self.max_length:
            object.__setattr__(self, "max_length", DEFAULT_MAX_LENGTH[self.type])

    @property
    def is_multiline(self) -> bool:
        return self.type in ("text", "lines", "rich")


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    fields: tuple[TextField, ...]
    note: str = ""


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    description: str
    sections: tuple[Section, ...]
    # Which public page the preview shows. "static" uses `preview_path` as-is;
    # "product"/"category" ask the admin blueprint to resolve a real URL at runtime
    # (the ids/slugs depend on the live catalogue).
    preview_path: str = "/"
    preview_kind: Literal["static", "product", "category"] = "static"
    # Index grouping only ("Sitio" vs "Páginas").
    category: str = "Sitio"
    icon: str = "✎"

    @property
    def fields(self) -> list[TextField]:
        return [f for section in self.sections for f in section.fields]


# --- tokens -------------------------------------------------------------------
# Placeholders any copy may embed. These resolve on every render; anything else is
# passed per call (e.g. t("category.meta.title", category="Tote")). The resolver is
# held to this list by a test, so the docs and the hints can't drift from reality.
GLOBAL_TOKENS: tuple[str, ...] = (
    "brand",
    "tagline",
    "year",
    "instagram_url",
    "instagram_handle",
    "whatsapp_number",
)

# The extra placeholders a SINGLE field may embed, because its one call site passes
# them (`t("category.meta.title", category=…)`). They exist nowhere else, so a
# validator that only knew GLOBAL_TOKENS would reject the copy that ships in this
# very file. Two tests hold this map to the real call sites in both directions.
FIELD_TOKENS: dict[str, tuple[str, ...]] = {
    "cart.line.remove_aria": ("title",),
    "category.empty": ("category_lower",),
    "category.heading_models": ("category",),
    "category.meta.description": ("category",),
    "category.meta.title": ("category",),
    "home.categories.soon_aria": ("category",),
    "product.gallery.thumb_aria": ("index", "total"),
    "product.meta.category_clause": ("category",),
    "product.meta.description": ("title", "category_clause", "price_clause"),
    "product.meta.price_clause": ("price",),
    "product.meta.title": ("title", "category"),
    "product.og.description": ("title", "price_clause"),
    "product.og.image_alt": ("title",),
    "product.og.price_clause": ("price",),
    "product.og.title": ("title",),
    "product.twitter.category_clause": ("category",),
    "product.twitter.description": ("title", "category_clause"),
    "seo.page_title_format": ("title",),
    "seo.product.description_fallback": ("title",),
    "whatsapp.message.product": ("title", "url"),
}


def allowed_tokens(key: str) -> tuple[str, ...]:
    """Every `{token}` this field may embed — its own first, then the site-wide ones.

    Field-first because that order is also what the admin reads back to the shop owner
    when she invents one, and `{title}` is the answer she is looking for.
    """
    return FIELD_TOKENS.get(key, ()) + GLOBAL_TOKENS


# --- groups -------------------------------------------------------------------

GLOBAL = Group(
    key="global",
    title="Marca y navegación",
    description="Lo que aparece en todas las páginas: la marca, el menú, el carrito y el pie.",
    icon="◈",
    preview_path="/",
    sections=(
        Section(
            key="brand",
            title="Marca",
            note="Estos valores se pueden reutilizar en cualquier otro texto escribiendo {brand} o {tagline}.",
            fields=(
                TextField("global.brand", "Nombre de la marca", "GLÜCK", max_length=60),
                TextField(
                    "global.tagline",
                    "Bajada de la marca",
                    "Bolsos minimalistas de cuero vegano",
                    hint="Se usa en el título del sitio, el hero y el pie.",
                ),
                TextField(
                    "global.instagram_url",
                    "Link de Instagram",
                    "https://www.instagram.com/gluck_bags/",
                    type="url",
                ),
                TextField("global.instagram_handle", "Usuario de Instagram", "@gluck_bags"),
            ),
        ),
        Section(
            key="whatsapp",
            title="WhatsApp",
            note=(
                "El botón flotante de WhatsApp aparece en todas las páginas. El mensaje se "
                "abre ya escrito en el chat: la clienta sólo tiene que enviarlo."
            ),
            fields=(
                TextField(
                    "global.whatsapp_number",
                    "Número de WhatsApp",
                    "+54 9 11 3605-3910",
                    hint=(
                        "Con código de país. Podés escribirlo con espacios, guiones o el 9 "
                        "de celular: el link se arma sólo con los dígitos. Vaciá el campo "
                        "para ocultar el botón."
                    ),
                ),
                TextField(
                    "whatsapp.aria",
                    "Botón flotante (lectores de pantalla)",
                    "Escribinos por WhatsApp",
                ),
                TextField("whatsapp.tooltip", "Globito del botón", "¿Consultas? Escribinos"),
                TextField(
                    "whatsapp.message.default",
                    "Mensaje para toda la web",
                    "¡Hola {brand}! Estoy viendo la web y quería hacerles una consulta.",
                    type="text",
                    hint="Es el mensaje que se abre desde cualquier página que no sea un producto.",
                ),
                TextField(
                    "whatsapp.message.product",
                    "Mensaje desde un producto",
                    "¡Hola {brand}! Me interesa el modelo {title} y quería hacerles una consulta. {url}",
                    type="text",
                    hint=(
                        "Sólo en la página de un bolso. {title} es el nombre del modelo y "
                        "{url} el link a su página."
                    ),
                ),
            ),
        ),
        Section(
            key="nav",
            title="Menú",
            fields=(
                TextField("nav.collection", "Colección", "Colección"),
                TextField("nav.bags", "Bolsos", "Bolsos"),
                TextField("nav.material", "Materia", "Materia"),
                TextField("nav.about", "Nosotras", "Nosotras"),
                TextField("nav.cta", "Botón de compra", "Comprar"),
                TextField(
                    "nav.home_aria",
                    "Etiqueta del logo (lectores de pantalla)",
                    "{brand} inicio",
                ),
                TextField("nav.cart_aria", "Botón del carrito (lectores de pantalla)", "Abrir carrito"),
                TextField("nav.menu_aria", "Botón del menú (lectores de pantalla)", "Abrir menú"),
            ),
        ),
        Section(
            key="cart_drawer",
            title="Carrito lateral",
            fields=(
                TextField("cart.drawer.eyebrow", "Antetítulo", "Tu selección"),
                TextField("cart.drawer.title", "Título", "Carrito"),
                TextField("cart.drawer.close_aria", "Botón cerrar (lectores de pantalla)", "Cerrar carrito"),
                TextField("cart.subtotal_label", "Etiqueta del subtotal", "Subtotal"),
                TextField(
                    "cart.checkout.note",
                    "Aviso de envío",
                    "El costo de envío se confirma en el checkout, antes de pagar.",
                ),
                TextField(
                    "cart.checkout.email_label",
                    "Etiqueta del email",
                    "Email para la confirmación del pedido",
                ),
                TextField("cart.checkout.email_placeholder", "Ejemplo dentro del campo", "tu@email.com"),
                TextField("cart.checkout.button", "Botón de checkout", "Finalizar compra"),
                TextField(
                    "cart.checkout.email_error",
                    "Error: falta el email",
                    "Ingresá tu email para recibir la confirmación del pedido.",
                ),
                TextField(
                    "cart.checkout.error",
                    "Error: no se pudo iniciar el checkout",
                    "No pudimos iniciar el checkout. Probá de nuevo en un momento.",
                ),
            ),
        ),
        Section(
            key="footer",
            title="Pie de página",
            fields=(
                TextField("footer.cta_eyebrow", "Antetítulo del cierre", "¿Te gustó algo?"),
                TextField("footer.cta_link", "Link destacado", "Seguinos en Instagram"),
                TextField("footer.link.about", "Link · Nosotras", "Nosotras"),
                TextField("footer.link.contact", "Link · Contacto", "Contacto"),
                TextField("footer.link.shipping", "Link · Envíos", "Envíos"),
                TextField("footer.link.returns", "Link · Cambios", "Cambios"),
                TextField("footer.link.instagram", "Link · Instagram", "Instagram"),
                TextField("footer.link.terms", "Link · Términos", "Términos"),
                TextField("footer.link.privacy", "Link · Privacidad", "Privacidad"),
                TextField(
                    "footer.copyright",
                    "Línea de copyright",
                    "© {year} {brand} · Gluck Bags. Todos los derechos reservados.",
                    hint="{year} se reemplaza por el año actual.",
                ),
                TextField("footer.back_to_top", "Volver arriba", "Volver arriba ↑"),
                TextField("footer.badges", "Sello del pie", "Cuero vegano · Hecho a mano"),
            ),
        ),
    ),
)


HOME = Group(
    key="home",
    title="Inicio",
    description="Toda la copia de la portada, de arriba hacia abajo.",
    icon="⌂",
    preview_path="/",
    sections=(
        Section(
            key="hero",
            title="Portada (hero)",
            fields=(
                TextField("home.hero.eyebrow", "Antetítulo", "Cuero vegano · Sin costuras"),
                TextField(
                    "home.hero.title",
                    "Título principal (H1)",
                    "{tagline}",
                    hint="Por defecto muestra la bajada de la marca. Podés escribir un texto propio.",
                ),
                TextField(
                    "home.hero.subtitle",
                    "Bajada",
                    "Bolsos de líneas puras, pensados para durar. Sin pieles, sin excesos.",
                    type="text",
                ),
                TextField("home.hero.cta_primary", "Botón principal", "Ver la colección"),
                TextField("home.hero.cta_secondary", "Botón secundario", "Comprar ahora"),
                TextField("home.hero.scroll_label", "Indicador de scroll", "Scroll"),
                TextField("home.hero.scroll_aria", "Indicador (lectores de pantalla)", "Bajar a la colección"),
                TextField(
                    "home.hero.image_alt",
                    "Descripción de la foto",
                    "Tote de cuero vegano cognac de {brand} en la playa",
                    hint="La leen Google y los lectores de pantalla. Describí lo que se ve.",
                ),
                TextField(
                    "home.hero.image",
                    "Foto de portada",
                    "/static/img/hero/hero-tote-cognac-playa.jpg",
                    type="image",
                    hint="Subí o pegá una imagen. Consejo: usá una foto grande y liviana "
                    "(hasta ~1600px). Mientras no la cambies, se sirve la versión "
                    "optimizada de origen.",
                ),
            ),
        ),
        Section(
            key="marquee",
            title="Cinta animada",
            fields=(
                TextField(
                    "home.marquee.phrases",
                    "Frases",
                    "Cuero vegano minimalista\nDe una sola pieza\nDiseño atemporal\nSin crueldad",
                    type="lines",
                    hint="Una frase por línea. Se repiten en bucle.",
                ),
            ),
        ),
        Section(
            key="categories",
            title="Grilla de categorías",
            fields=(
                TextField("home.categories.eyebrow", "Antetítulo", "La colección"),
                TextField(
                    "home.categories.title",
                    "Título",
                    "Cuatro siluetas, infinitas combinaciones",
                ),
                TextField("home.categories.soon", "Etiqueta sin stock", "Próximamente"),
                TextField(
                    "home.categories.soon_aria",
                    "Sin stock (lectores de pantalla)",
                    "{category} — próximamente",
                ),
            ),
        ),
        Section(
            key="feature",
            title="Bloque destacado",
            fields=(
                TextField("home.feature.eyebrow", "Antetítulo", "Diseño de autor"),
                TextField(
                    "home.feature.title",
                    "Título",
                    "El Tote que te sigue<br>a todas partes",
                    type="rich",
                    max_length=300,
                    hint="Podés cortar la línea con <br>.",
                ),
                TextField(
                    "home.feature.body",
                    "Texto",
                    "Cada bolso sale de una sola pieza de cuero vegano: la diseñamos, la "
                    "cortamos con láser y la plegamos a mano. Sin costuras — la forma se "
                    "sostiene con remaches y broches.",
                    type="text",
                ),
                TextField(
                    "home.feature.specs",
                    "Lista de características",
                    "De una sola pieza, sin costuras\n"
                    "Cortada con láser desde un molde propio\n"
                    "Plegada y unida con remaches y broches",
                    type="lines",
                    hint="Un ítem por línea.",
                ),
                TextField("home.feature.cta", "Botón", "Quiero el mío"),
                TextField(
                    "home.feature.image_alt",
                    "Descripción de la foto",
                    "Tote {brand} en la cubierta de un velero",
                ),
                TextField(
                    "home.feature.image",
                    "Foto de la sección",
                    "/static/img/hero/hero-tote-gris-velero.jpg",
                    type="image",
                    hint="Mientras no la cambies, se sirve la versión optimizada de origen.",
                ),
            ),
        ),
        Section(
            key="shop",
            title="Grilla de productos",
            fields=(
                TextField("home.shop.eyebrow", "Antetítulo", "Bolsos"),
                TextField("home.shop.title", "Título", "Lo último de {brand}"),
                TextField(
                    "home.shop.empty",
                    "Mensaje sin productos",
                    "Muy pronto vas a ver acá nuestros bolsos.",
                    type="text",
                ),
                TextField("home.shop.badge_video", "Etiqueta con video", "▶ Ver video"),
                TextField("home.shop.badge_more", "Etiqueta sin video", "Ver más"),
            ),
        ),
        Section(
            key="materia",
            title="Bloque de video (La materia)",
            fields=(
                TextField("home.materia.eyebrow", "Antetítulo", "La materia"),
                TextField(
                    "home.materia.title",
                    "Título",
                    "Cuero vegano,<br>elegido a mano",
                    type="rich",
                    max_length=300,
                ),
                TextField(
                    "home.materia.body",
                    "Texto",
                    "Seleccionamos cada rollo por su tacto y su color. Materiales libres de "
                    "origen animal que envejecen con gracia, para que cada bolso cuente su "
                    "propia historia.",
                    type="text",
                ),
                TextField(
                    "home.materia.caption",
                    "Epígrafe",
                    "Detrás de escena · selección de materiales",
                ),
                TextField(
                    "home.materia.poster",
                    "Imagen del video (poster)",
                    "/static/img/video-posters/cuero-materia-prima.jpg",
                    type="image",
                    hint="La imagen fija que se ve antes de que cargue el video.",
                ),
            ),
        ),
        Section(
            key="manifesto",
            title="Manifiesto",
            fields=(
                TextField("home.manifesto.eyebrow", "Antetítulo", "Nosotras"),
                TextField(
                    "home.manifesto.quote",
                    "Cita",
                    "Creemos en lo simple, en lo que dura y en lo que no le hace daño a nadie. "
                    "Cada <em>{brand}</em> nace de esa idea: belleza sin culpa.",
                    type="rich",
                    max_length=600,
                ),
                TextField("home.manifesto.value1_title", "Valor 1 · título", "100% vegano"),
                TextField(
                    "home.manifesto.value1_body",
                    "Valor 1 · texto",
                    "Sin pieles ni derivados animales.",
                    type="text",
                ),
                TextField("home.manifesto.value2_title", "Valor 2 · título", "Sin costuras"),
                TextField(
                    "home.manifesto.value2_body",
                    "Valor 2 · texto",
                    "De una sola pieza, plegada y unida con remaches.",
                    type="text",
                ),
                TextField("home.manifesto.value3_title", "Valor 3 · título", "Atemporal"),
                TextField(
                    "home.manifesto.value3_body",
                    "Valor 3 · texto",
                    "Diseños que no pasan de moda.",
                    type="text",
                ),
            ),
        ),
        Section(
            key="closer",
            title="Cierre (packaging)",
            fields=(
                TextField("home.closer.eyebrow", "Antetítulo", "Listo para regalar"),
                TextField(
                    "home.closer.title",
                    "Título",
                    "Cada pedido llega<br>como un regalo",
                    type="rich",
                    max_length=300,
                ),
                TextField(
                    "home.closer.body",
                    "Texto",
                    "Empaque kraft con el sello {brand}. Hacé tu pedido en la tienda online y "
                    "lo recibís listo para regalar; si tenés dudas, te acompañamos por Instagram.",
                    type="text",
                ),
                TextField(
                    "home.closer.specs",
                    "Lista de características",
                    "Envíos a todo el país\n"
                    "Packaging de regalo incluido\n"
                    "Atención personalizada por Instagram",
                    type="lines",
                ),
                TextField("home.closer.cta", "Botón", "Ver los bolsos"),
                TextField(
                    "home.closer.poster",
                    "Imagen del video (poster)",
                    "/static/img/video-posters/packaging-unboxing.jpg",
                    type="image",
                    hint="La imagen fija que se ve antes de que cargue el video.",
                ),
            ),
        ),
    ),
)


PRODUCT = Group(
    key="product",
    title="Página de producto",
    description="Etiquetas, botones y metadatos de la ficha de cada bolso.",
    icon="◧",
    preview_path="/",
    preview_kind="product",
    sections=(
        Section(
            key="labels",
            title="Ficha",
            fields=(
                TextField("product.breadcrumb_home", "Miga de pan · inicio", "Inicio"),
                TextField(
                    "product.price_fallback",
                    "Precio sin definir",
                    "Consultar",
                    hint="Se muestra cuando el producto no tiene precio cargado.",
                ),
                TextField("product.cta_add", "Botón agregar", "Agregar al carrito"),
                TextField("product.cta_alt", "Link secundario", "o consultá por Instagram"),
                TextField(
                    "product.cta_consult",
                    "Botón sin precio",
                    "Consultar por Instagram",
                ),
                TextField("product.category_nav_label", "Título de las categorías", "Explorá por categoría"),
                TextField("product.related.eyebrow", "Relacionados · antetítulo", "Seguí mirando"),
                TextField("product.related.title", "Relacionados · título", "Otros bolsos"),
            ),
        ),
        Section(
            key="gallery",
            title="Galería",
            fields=(
                TextField("product.gallery.prev_aria", "Flecha anterior", "Imagen anterior"),
                TextField("product.gallery.next_aria", "Flecha siguiente", "Imagen siguiente"),
                TextField("product.gallery.thumbs_aria", "Miniaturas", "Imágenes del producto"),
                TextField(
                    "product.gallery.thumb_aria",
                    "Miniatura",
                    "Ver imagen {index} de {total}",
                ),
            ),
        ),
        Section(
            key="meta",
            title="Google y redes",
            note="Podés usar {title}, {category}, {price} y {brand}.",
            fields=(
                TextField(
                    "product.meta.title",
                    "Título en Google",
                    "{title} · {category} de cuero vegano | {brand}",
                ),
                TextField(
                    "product.meta.category_fallback",
                    "Categoría por defecto",
                    "Cartera",
                    hint="Se usa cuando el producto no tiene categoría.",
                ),
                TextField(
                    "product.meta.description",
                    "Descripción en Google",
                    "{title}{category_clause} de cuero vegano hecho a mano por {brand}. "
                    "Diseño minimalista, sin crueldad animal.{price_clause}",
                    type="text",
                ),
                TextField(
                    "product.meta.category_clause",
                    "Fragmento con categoría",
                    ", {category}",
                    hint="Se inserta en la descripción sólo si el producto tiene categoría.",
                ),
                TextField(
                    "product.meta.price_clause",
                    "Fragmento con precio",
                    " {price}. Comprá online con envío a todo el país.",
                ),
                TextField(
                    "product.meta.no_price_clause",
                    "Fragmento sin precio",
                    " Consultá disponibilidad por Instagram.",
                ),
                TextField("product.og.title", "Título al compartir", "{title} · {brand}"),
                TextField(
                    "product.og.description",
                    "Descripción al compartir",
                    "{title}{price_clause}. {brand}, cuero vegano hecho a mano.",
                    type="text",
                ),
                TextField("product.og.price_clause", "Compartir · fragmento con precio", " — {price}"),
                TextField(
                    "product.twitter.description",
                    "Descripción en Twitter/X",
                    "{title}{category_clause} — {brand}, cuero vegano hecho a mano.",
                    type="text",
                ),
                TextField("product.twitter.category_clause", "Twitter/X · fragmento con categoría", " · {category}"),
                TextField("product.og.image_alt", "Descripción de la imagen al compartir", "{title} — {brand}"),
            ),
        ),
    ),
)


CATEGORY = Group(
    key="category",
    title="Categorías",
    description="Los cuatro tipos de bolso: cómo se llaman en la web y qué dice cada página.",
    icon="◫",
    preview_path="/categoria/tote",
    preview_kind="category",
    sections=(
        Section(
            key="page",
            title="Página de categoría",
            note="Podés usar {category} y {brand}.",
            fields=(
                TextField("category.eyebrow", "Antetítulo", "La colección"),
                TextField(
                    "category.meta.title",
                    "Título en Google",
                    "{category} · Carteras de cuero vegano | {brand}",
                ),
                TextField(
                    "category.meta.description",
                    "Descripción en Google",
                    "{category}: carteras y bolsos de cuero vegano hechos a mano de {brand}. "
                    "Diseño minimalista y atemporal.",
                    type="text",
                ),
                TextField(
                    "category.heading_models",
                    "Título oculto de la grilla",
                    "Modelos de {category}",
                    hint="No se ve en pantalla; lo leen Google y los lectores de pantalla.",
                ),
                TextField(
                    "category.empty",
                    "Mensaje sin productos",
                    "Muy pronto vas a ver acá nuestros {category_lower}.",
                    type="text",
                ),
                TextField("category.back_link", "Link de vuelta", "← Ver todos los bolsos"),
            ),
        ),
        Section(
            key="tote",
            title="Tote",
            note="El nombre define la dirección web (/categoria/tote) y no cambia; acá editás cómo se muestra.",
            fields=(
                TextField("category.tote.label", "Nombre visible", "Tote"),
                TextField("category.tote.tagline", "Bajada en la portada", "Para todos los días"),
                TextField(
                    "category.tote.intro",
                    "Introducción de la página",
                    "El bolso de todos los días: amplio, de líneas rectas y hecho de una sola "
                    "pieza de cuero vegano. Pensado para que entre todo sin perder la silueta "
                    "minimalista y atemporal que define a {brand}.",
                    type="text",
                ),
            ),
        ),
        Section(
            key="mini-bag",
            title="Mini Bag",
            fields=(
                TextField("category.mini-bag.label", "Nombre visible", "Mini Bag"),
                TextField("category.mini-bag.tagline", "Bajada en la portada", "Lo esencial, en pequeño"),
                TextField(
                    "category.mini-bag.intro",
                    "Introducción de la página",
                    "Lo esencial, en formato pequeño. Bandoleras y crossbody de cuero vegano "
                    "para llevar lo justo con las manos libres, sin resignar diseño ni carácter.",
                    type="text",
                ),
            ),
        ),
        Section(
            key="bucket-bag",
            title="Bucket Bag",
            fields=(
                TextField("category.bucket-bag.label", "Nombre visible", "Bucket Bag"),
                TextField("category.bucket-bag.tagline", "Bajada en la portada", "Volumen y carácter"),
                TextField(
                    "category.bucket-bag.intro",
                    "Introducción de la página",
                    "Volumen y carácter en una silueta tipo balde. Cuero vegano plegado a mano, "
                    "espacioso y con una caída suave que la vuelve inconfundible.",
                    type="text",
                ),
            ),
        ),
        Section(
            key="clutch",
            title="Clutch",
            fields=(
                TextField("category.clutch.label", "Nombre visible", "Clutch"),
                TextField("category.clutch.tagline", "Bajada en la portada", "Lo justo y necesario"),
                TextField(
                    "category.clutch.intro",
                    "Introducción de la página",
                    "Lo justo y necesario para una salida. Sobres y clutches de cuero vegano, "
                    "mínimos y elegantes, hechos a mano y sin crueldad animal.",
                    type="text",
                ),
            ),
        ),
    ),
)


CART = Group(
    key="cart",
    title="Carrito y compra",
    description="La página del carrito y la pantalla de gracias posterior al pago.",
    icon="⛁",
    preview_path="/carrito",
    sections=(
        Section(
            key="page",
            title="Página del carrito",
            fields=(
                TextField("cart.page.title_tag", "Título en Google", "Tu carrito · {brand}"),
                TextField(
                    "cart.page.meta_description",
                    "Descripción en Google",
                    "Revisá los bolsos que agregaste a tu carrito en {brand} y finalizá tu compra.",
                    type="text",
                ),
                TextField("cart.page.eyebrow", "Antetítulo", "Tu selección"),
                TextField("cart.page.title", "Título", "Carrito"),
                TextField("cart.page.empty", "Carrito vacío", "Tu carrito está vacío."),
                TextField("cart.page.empty_cta", "Botón del carrito vacío", "Ver bolsos"),
                TextField("cart.page.keep_shopping", "Seguir mirando", "← Seguir mirando"),
                TextField("cart.line.remove", "Quitar producto", "Quitar"),
                TextField("cart.line.remove_aria", "Quitar (lectores de pantalla)", "Quitar {title}"),
                TextField("cart.line.qty_dec_aria", "Restar unidad", "Quitar uno"),
                TextField("cart.line.qty_inc_aria", "Sumar unidad", "Agregar uno"),
            ),
        ),
        Section(
            key="thanks",
            title="Gracias por tu compra",
            fields=(
                TextField("thanks.title_tag", "Título en Google", "¡Gracias por tu compra! · {brand}"),
                TextField(
                    "thanks.meta_description",
                    "Descripción en Google",
                    "Recibimos tu pedido en {brand}. Tu pago se procesa en Tienda Nube y te "
                    "llega la confirmación y el seguimiento del envío por email.",
                    type="text",
                ),
                TextField("thanks.eyebrow", "Antetítulo", "Pedido confirmado"),
                TextField("thanks.title", "Título", "¡Gracias por tu compra!"),
                TextField(
                    "thanks.body",
                    "Texto",
                    "Tu pago se está procesando en Tienda Nube. Cuando se acredite vas a recibir "
                    "la confirmación por email con el detalle y el seguimiento del envío.",
                    type="text",
                ),
                TextField(
                    "thanks.help",
                    "Ayuda",
                    "¿Alguna duda con tu pedido? Escribinos y te ayudamos.",
                    type="text",
                ),
                TextField("thanks.cta_primary", "Botón principal", "Seguir mirando"),
                TextField("thanks.cta_secondary", "Botón secundario", "Escribinos por Instagram"),
            ),
        ),
    ),
)


SEO = Group(
    key="seo",
    title="Google y redes",
    description="El título y la descripción de la portada, y la tarjeta que se ve al compartir el link.",
    icon="◎",
    preview_path="/",
    sections=(
        Section(
            key="home",
            title="Portada",
            fields=(
                TextField(
                    "seo.home.title",
                    "Título en Google",
                    "{brand} (Gluck Bags) · Carteras de cuero vegano hechas a mano",
                    hint="Lo ideal es hasta 60 caracteres, con la marca al principio.",
                ),
                TextField(
                    "seo.home.description",
                    "Descripción en Google",
                    "{brand} (Gluck Bags): carteras y bolsos de cuero vegano hechos a mano en "
                    "Argentina. Diseño de autor, minimalista y sin costuras.",
                    type="text",
                    hint="Lo ideal es entre 120 y 160 caracteres.",
                ),
                TextField(
                    "seo.og.image_alt",
                    "Descripción de la imagen al compartir",
                    "Tote de cuero vegano cognac de {brand} en la playa",
                ),
                TextField(
                    "seo.og.image",
                    "Imagen al compartir el link",
                    "/static/img/og-image.jpg",
                    type="image",
                    hint="1200×630. Es lo que aparece al compartir el sitio en WhatsApp, "
                    "Instagram o Google.",
                ),
                TextField(
                    "seo.page_title_format",
                    "Formato del título de las páginas",
                    "{title} · {brand}",
                    hint="Cómo se arma el título de Nosotras, Contacto, Envíos, etc.",
                ),
            ),
        ),
        Section(
            key="structured",
            title="Ficha del negocio",
            note="No se ve en la web: es lo que Google lee para entender la marca.",
            fields=(
                TextField(
                    "seo.organization.description",
                    "Descripción del negocio",
                    "Bolsos y carteras de cuero vegano hechos a mano en Argentina, con "
                    "diseño minimalista y atemporal.",
                    type="text",
                ),
                TextField(
                    "seo.organization.alternate_names",
                    "Otras formas de escribir la marca",
                    "Gluck, Gluck Bags, Glück",
                    hint="Separadas por coma. Así Google sabe que quien busca "
                    "\"gluck\" o \"gluck bags\" está buscando esta marca.",
                ),
                TextField(
                    "seo.product.description_fallback",
                    "Descripción de un producto sin texto propio",
                    "{title} — {brand}, cartera de cuero vegano hecha a mano.",
                    type="text",
                ),
            ),
        ),
    ),
)


ERRORS = Group(
    key="errors",
    title="Página no encontrada",
    description="Lo que ve alguien que entra a un link roto (error 404).",
    icon="⚠",
    preview_path="/404",
    sections=(
        Section(
            key="not_found",
            title="Error 404",
            fields=(
                TextField("error404.title_tag", "Título en Google", "Página no encontrada · {brand}"),
                TextField("error404.eyebrow", "Antetítulo", "Error 404"),
                TextField("error404.title", "Título", "Esta página no existe"),
                TextField(
                    "error404.body",
                    "Texto",
                    "El enlace que seguiste no lleva a ninguna parte, o la página se movió.",
                    type="text",
                ),
                TextField("error404.cta_primary", "Botón principal", "Volver al inicio"),
                TextField("error404.cta_secondary", "Botón secundario", "Ver los bolsos"),
            ),
        ),
    ),
)


def _editorial_page(
    key: str,
    path: str,
    title: str,
    description: str,
    body: str,
    icon: str = "▤",
) -> Group:
    """One of the six editorial/legal pages. They share the exact same shape:
    a heading, the Google description and the page body as rich text."""
    slug = key.split("-", 1)[1]
    return Group(
        key=key,
        title=title,
        description=f"Contenido de la página {path}",
        category="Páginas",
        icon=icon,
        preview_path=path,
        sections=(
            Section(
                key="content",
                title=title,
                note="En el cuerpo podés usar títulos, listas, negritas y links.",
                fields=(
                    TextField(f"page.{slug}.title", "Título de la página", title),
                    TextField(
                        f"page.{slug}.meta_description",
                        "Descripción en Google",
                        description,
                        type="text",
                        hint="Lo ideal es entre 120 y 160 caracteres.",
                    ),
                    TextField(f"page.{slug}.body", "Cuerpo", body, type="rich"),
                ),
            ),
        ),
    )


PAGE_ABOUT = _editorial_page(
    "page-about",
    "/nosotras",
    "Nosotras",
    "Quiénes somos: {brand}, bolsos de cuero vegano hechos a mano en Argentina, con "
    "diseño minimalista y atemporal.",
    """<p>{brand} nació de una idea simple: bolsos de líneas puras, hechos para durar, sin pieles de origen animal y sin excesos. Diseñamos cada pieza en Argentina y la producimos de forma artesanal, en series cortas.</p>
<h2>Cómo los hacemos</h2>
<p>Cada bolso sale de una sola pieza de cuero vegano: la diseñamos, la cortamos con láser desde un molde propio y la plegamos a mano. La forma se sostiene con remaches y broches, no con costuras. Por eso cada modelo es limpio, resistente y atemporal.</p>
<h2>Cuero vegano, elegido a mano</h2>
<p>Seleccionamos cada material por su tacto, su color y su durabilidad. Trabajamos con materiales libres de crueldad animal, buscando el mejor equilibrio entre estética y uso diario.</p>
<p>¿Querés conocer la colección? Escribinos por <a href="{instagram_url}" target="_blank" rel="noopener noreferrer">Instagram</a> o pasá por la <a href="/#shop">tienda</a>.</p>""",
)

PAGE_CONTACT = _editorial_page(
    "page-contact",
    "/contacto",
    "Contacto",
    "Escribinos por Instagram y te ayudamos a elegir tu bolso {brand}. Las compras se "
    "hacen online en la tienda del sitio.",
    """<p>¿Tenés una consulta sobre un bolso, un envío o un pedido especial? Estamos para ayudarte. Te respondemos lo antes posible.</p>
<h2>Canales</h2>
<ul>
<li><strong>WhatsApp:</strong> {whatsapp_number} — tocá el botón verde que está abajo a la derecha en cualquier página y el mensaje se abre solo.</li>
<li><strong>Instagram:</strong> <a href="{instagram_url}" target="_blank" rel="noopener noreferrer">{instagram_handle}</a> — consultas y asesoramiento personalizado por mensaje directo.</li>
</ul>
<h2>Horarios de atención</h2>
<p>Respondemos de lunes a viernes. Las compras se hacen directamente en la <a href="/#shop">tienda online</a>; si necesitás ayuda con un pedido, escribinos con tu número de pedido.</p>""",
)

PAGE_SHIPPING = _editorial_page(
    "page-shipping",
    "/envios",
    "Envíos",
    "Hacemos envíos a todo el país. Conocé tiempos, costos y cómo se despacha tu pedido {brand}.",
    """<p>Hacemos envíos a todo el país. Despachamos tu pedido una vez que el pago se acredita en el checkout.</p>
<h2>Tiempos y costos</h2>
<ul>
<li><strong>Zona:</strong> envíos a todo el país.</li>
<li><strong>Tiempo estimado:</strong> depende del destino y del correo; te lo informamos al confirmar el despacho.</li>
<li><strong>Costo:</strong> lo ves en el checkout antes de confirmar la compra.</li>
</ul>
<h2>Seguimiento</h2>
<p>Cuando despachamos tu pedido te enviamos el código de seguimiento por email, a la dirección que usaste en la compra.</p>
<p>¿Dudas con tu envío? Escribinos por <a href="{instagram_url}" target="_blank" rel="noopener noreferrer">Instagram</a>.</p>""",
)

PAGE_RETURNS = _editorial_page(
    "page-returns",
    "/cambios-y-devoluciones",
    "Cambios y devoluciones",
    "Política de cambios y devoluciones de {brand}: plazos, condiciones y cómo gestionarlos.",
    """<p>Queremos que estés feliz con tu bolso. Si algo no salió como esperabas, escribinos y lo resolvemos.</p>
<h2>Cambios</h2>
<p>Aceptamos cambios sobre productos sin uso y en las mismas condiciones en que se entregaron, dentro de los <strong>30 días</strong> de recibido tu pedido. Escribinos apenas lo recibas y coordinamos el cambio.</p>
<h2>Devoluciones</h2>
<p>Si te arrepentís de tu compra, tenés <strong>10 días corridos</strong> desde que recibís el producto para pedir la devolución (derecho de revocación para compras online, Ley 24.240). Te explicamos cómo gestionarla y, una vez recibido y revisado el bolso, te reintegramos el pago.</p>
<h2>Cómo iniciarlo</h2>
<p>Escribinos por <a href="{instagram_url}" target="_blank" rel="noopener noreferrer">Instagram</a> con tu número de pedido y te guiamos en el proceso.</p>""",
)

PAGE_TERMS = _editorial_page(
    "page-terms",
    "/terminos",
    "Términos y condiciones",
    "Términos y condiciones de uso y de compra del sitio de {brand}: cómo comprar online, "
    "medios de pago, envíos y las condiciones generales del servicio.",
    """<p>Estos términos y condiciones regulan el uso del sitio de {brand} y la compra de nuestros productos. Al navegar o comprar, aceptás estas condiciones.</p>
<h2>Productos y precios</h2>
<p>Las imágenes son ilustrativas. Al tratarse de piezas artesanales pueden existir pequeñas variaciones de color y terminación. Los precios están publicados en el sitio y pueden cambiar sin previo aviso; el valor final de tu compra es el que se muestra en el checkout al confirmar el pedido.</p>
<h2>Compras</h2>
<p>Las compras se realizan online en este sitio: agregás tus productos al carrito y completás el pago en el checkout seguro de Tienda Nube. La compra queda confirmada cuando el pago se acredita, y recibís la confirmación del pedido por email. Si tenés dudas antes de comprar, podés escribirnos por Instagram.</p>
<h2>Pagos</h2>
<p>El pago se procesa a través de la plataforma Tienda Nube y sus medios de pago habilitados. {brand} no recibe ni almacena los datos de tu tarjeta.</p>
<h2>Propiedad intelectual</h2>
<p>Las marcas, textos, fotografías y diseños del sitio pertenecen a {brand} y no pueden reproducirse sin autorización.</p>
<h2>Contacto</h2>
<p>Ante cualquier duda, escribinos por <a href="{instagram_url}" target="_blank" rel="noopener noreferrer">Instagram</a> o desde la página de <a href="/contacto">contacto</a>.</p>""",
)

PAGE_PRIVACY = _editorial_page(
    "page-privacy",
    "/privacidad",
    "Política de privacidad",
    "Cómo {brand} trata tus datos personales y qué herramientas de medición usa el sitio.",
    """<p>En {brand} cuidamos tus datos. Esta política explica qué información tratamos cuando navegás o nos contactás, y para qué.</p>
<h2>Qué datos tratamos</h2>
<p>Los datos que nos compartís voluntariamente al contactarnos y los necesarios para procesar tu compra: tu email (para enviarte la confirmación del pedido) y, en el checkout, tu nombre, dirección de envío y datos de contacto.</p>
<h2>Carrito y cookies</h2>
<p>El carrito usa una cookie de sesión propia, sólo para recordar los productos que agregaste. No usamos cookies publicitarias ni de seguimiento entre sitios.</p>
<h2>Pagos y Tienda Nube</h2>
<p>La compra se completa en el checkout de <a href="https://www.tiendanube.com/" target="_blank" rel="noopener noreferrer">Tienda Nube</a>, que procesa tus datos de pedido y de pago según su propia <a href="https://www.tiendanube.com/politicas-de-privacidad" target="_blank" rel="noopener noreferrer">política de privacidad</a>. Los datos de tu tarjeta nunca pasan por este sitio.</p>
<h2>Medición del sitio</h2>
<p>Usamos herramientas de analítica web respetuosas de la privacidad para entender, de forma agregada y anónima, cómo se usa el sitio y así mejorarlo. No las usamos para identificarte personalmente.</p>
<h2>Tus derechos</h2>
<p>Podés pedirnos acceder, rectificar o eliminar tus datos en cualquier momento escribiéndonos por <a href="{instagram_url}" target="_blank" rel="noopener noreferrer">Instagram</a> o desde la página de <a href="/contacto">contacto</a>.</p>""",
)


GROUPS: tuple[Group, ...] = (
    GLOBAL,
    HOME,
    PRODUCT,
    CATEGORY,
    CART,
    SEO,
    ERRORS,
    PAGE_ABOUT,
    PAGE_CONTACT,
    PAGE_SHIPPING,
    PAGE_RETURNS,
    PAGE_TERMS,
    PAGE_PRIVACY,
)

# Flat lookups, built once at import.
GROUPS_BY_KEY: dict[str, Group] = {group.key: group for group in GROUPS}
FIELDS: dict[str, TextField] = {f.key: f for group in GROUPS for f in group.fields}
DEFAULTS: dict[str, str] = {key: f.default for key, f in FIELDS.items()}
FIELD_GROUP: dict[str, str] = {
    f.key: group.key for group in GROUPS for f in group.fields
}
# Field -> the title of the card it lives in, for the visual editor's side panel.
FIELD_SECTION: dict[str, str] = {
    f.key: section.title for group in GROUPS for section in group.sections for f in section.fields
}

# The curated categories, in home-grid order. The name is the canonical identity
# (URL slug + the value stored on products); only the label is editable.
CURATED_CATEGORY_SLUGS: tuple[str, ...] = ("tote", "mini-bag", "bucket-bag", "clutch")

# The fields behind {brand}, {tagline}, … They reach the page through the context
# processor rather than a t() call, and they affect EVERY page, so the visual editor
# always lists them even when a page renders none of them directly.
TOKEN_FIELDS: tuple[str, ...] = (
    "global.brand",
    "global.tagline",
    "global.instagram_url",
    "global.instagram_handle",
    "global.whatsapp_number",
)


def group_for(key: str) -> Group | None:
    return GROUPS_BY_KEY.get(key)


def field_for(key: str) -> TextField | None:
    return FIELDS.get(key)


def groups_by_category() -> dict[str, list[Group]]:
    """Groups bucketed for the admin index, preserving declaration order."""
    buckets: dict[str, list[Group]] = {}
    for group in GROUPS:
        buckets.setdefault(group.category, []).append(group)
    return buckets


__all__ = [
    "CURATED_CATEGORY_SLUGS",
    "DEFAULTS",
    "FIELDS",
    "FIELD_GROUP",
    "FIELD_SECTION",
    "FIELD_TOKENS",
    "GLOBAL_TOKENS",
    "TOKEN_FIELDS",
    "GROUPS",
    "GROUPS_BY_KEY",
    "Group",
    "Section",
    "TextField",
    "allowed_tokens",
    "field_for",
    "group_for",
    "groups_by_category",
]
