import NextLink from "next/link";
import type { ComponentProps } from "react";

type Props = Omit<ComponentProps<"a">, "href"> & { href: ComponentProps<typeof NextLink>["href"] };

/** Native fragments preserve exact URLs across cached routes and keyboard navigation. */
export default function HashLink({ href, ...props }: Props) {
  return typeof href === "string" && href.includes("#")
    ? <a {...props} href={href} />
    : <NextLink {...props} href={href} />;
}
